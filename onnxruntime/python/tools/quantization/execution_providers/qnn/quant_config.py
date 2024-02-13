# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnx

from ...calibrate import CalibrationDataReader, CalibrationMethod
from ...quant_utils import QuantType
from ...quantize import StaticQuantConfig

Q16_TYPES = {QuantType.QInt16, QuantType.QUInt16}
Q8_TYPES = {QuantType.QInt8, QuantType.QUInt8}
OP_TYPES_TO_EXCLUDE = {"Cast"}


def _is_tensor_quantizable(tensor_name, value_infos, name_to_initializer):
    weight = name_to_initializer.get(tensor_name)
    if weight is not None:
        if weight.data_type == onnx.TensorProto.FLOAT:
            return True
    elif tensor_name in value_infos:
        vi = value_infos[tensor_name]
        if vi.type.HasField("tensor_type") and vi.type.tensor_type.elem_type == onnx.TensorProto.FLOAT:
            return True

    return False


def _get_output_activation_qtype(tensor_name, tensor_quant_overrides, activation_type):
    if tensor_name not in tensor_quant_overrides:
        return activation_type

    override_list = tensor_quant_overrides[tensor_name]
    if len(override_list) > 1:
        raise ValueError("Do not yet support per-tensor quantization")

    return override_list[0].get("quant_type", activation_type)


def _set_weight_type(tensor_quant_overrides, tensor_name, weight_type, weight_symmetric):
    if tensor_name not in tensor_quant_overrides or not tensor_quant_overrides[tensor_name]:
        tensor_quant_overrides[tensor_name] = [{}]

    if len(tensor_quant_overrides[tensor_name]) > 1:
        raise ValueError("Do not yet support per-tensor quantization")

    overrides = tensor_quant_overrides[tensor_name][0]

    # Only override if not initially overrided by user.
    if "quant_type" not in overrides and "symmetric" not in overrides:
        overrides["quant_type"] = weight_type
        overrides["symmetric"] = weight_symmetric


def _set_scale_zero_point(tensor_quant_overrides, tensor_name, quant_type, scale, zero_point):
    if tensor_name not in tensor_quant_overrides or not tensor_quant_overrides[tensor_name]:
        tensor_quant_overrides[tensor_name] = [{}]

    if len(tensor_quant_overrides[tensor_name]) > 1:
        raise ValueError("Do not yet support per-tensor quantization")

    overrides = tensor_quant_overrides[tensor_name][0]

    if "quant_type" not in overrides:
        overrides["quant_type"] = quant_type

    assert overrides["quant_type"] == quant_type, "Unexpected quant_type when overridding zp/scale"

    if set(overrides).intersection({"scale", "zero_point", "symmetric", "reduce_range", "rmin", "rmax"}):
        print(
            f"[WARNING]: Need to override zero-point/scale for tensor {tensor_name}, but you've already provided overrides!"
        )
    else:
        overrides["zero_point"] = zero_point
        overrides["scale"] = scale


def _add_to_convert_recv_nodes(tensor_quant_overrides, tensor_name, prod_type, consumer_type, consumer_names):
    if tensor_name not in tensor_quant_overrides or not tensor_quant_overrides[tensor_name]:
        tensor_quant_overrides[tensor_name] = [{"quant_type": prod_type}]

    overrides = tensor_quant_overrides[tensor_name][0]

    if "convert" not in overrides:
        overrides["convert"] = {"quant_type": consumer_type}

    convert_dict = overrides["convert"]
    assert consumer_type == convert_dict["quant_type"], "Consumer type doesn't match convert type"

    if "recv_nodes" not in convert_dict:
        convert_dict["recv_nodes"] = set()

    convert_dict["recv_nodes"].update(consumer_names)


def _check_not_in_convert_recv_nodes(tensor_quant_overrides, tensor_name, consumer_names):
    if tensor_name not in tensor_quant_overrides or not tensor_quant_overrides[tensor_name]:
        return True

    overrides = tensor_quant_overrides[tensor_name][0]

    if "convert" not in overrides:
        return True

    convert_dict = overrides["convert"]

    if "recv_nodes" not in convert_dict:
        return True

    return not convert_dict["recv_nodes"].intersection(consumer_names)


def _get_input_activation_qtype(tensor_quant_overrides, input_name, node_name, activation_type):
    if input_name not in tensor_quant_overrides or not tensor_quant_overrides[input_name]:
        return activation_type

    overrides = tensor_quant_overrides[input_name][0]
    producer_type = overrides.get("quant_type", activation_type)

    if "convert" not in overrides:
        return producer_type

    convert_dict = overrides["convert"]

    if "recv_nodes" not in convert_dict:
        return convert_dict["quant_type"]  # All consumers converted to the quant_type

    # Only specific consumers get the converted quant_type
    return convert_dict["quant_type"] if node_name in overrides["convert"]["recv_nodes"] else producer_type


@dataclass
class TensorTypeRequest:
    producer_type: QuantType | None
    consumers: tuple[QuantType, set[str]] | None


def _add_qtype_converts(
    tensor_quant_overrides, activation_type, value_infos, name_to_initializer, producers, consumers
):
    type_requests = {}

    # Scan tensor overrides for type conversion requests.
    for tensor_name, override_list in tensor_quant_overrides.items():
        if not _is_tensor_quantizable(tensor_name, value_infos, name_to_initializer):
            continue  # Skip non-quantizable tensors (e.g., not a float)

        if tensor_name in name_to_initializer:
            continue  # Skip initializers

        if not override_list or len(override_list) > 1:
            continue  # Skip per-channel stuff

        override = override_list[0]
        quant_type = override.get("quant_type", activation_type)
        node = producers[tensor_name]

        if quant_type != activation_type and "convert" not in override:
            # Add producer side of the type request
            if tensor_name not in type_requests:
                type_requests[tensor_name] = TensorTypeRequest(quant_type, None)
            else:
                if type_requests[tensor_name].producer_type is not None:
                    raise ValueError(f"Tensor {tensor_name} has multiple types.")

                type_requests[tensor_name].producer_type = quant_type

            # Add the consumer side of the type request
            for input_name in node.input:
                if (
                    input_name
                    and input_name not in name_to_initializer
                    and _is_tensor_quantizable(input_name, value_infos, name_to_initializer)
                ):
                    if input_name not in type_requests:
                        type_requests[input_name] = TensorTypeRequest(None, None)

                    if type_requests[input_name].consumers is None:
                        type_requests[input_name].consumers = (quant_type, set())

                    if type_requests[input_name].consumers[0] != quant_type:
                        raise ValueError(f"Tensor {input_name} has consumers requesting different types.")

                    type_requests[input_name].consumers[1].add(node.name)

    # Process type requests.
    for tensor_name, type_req in type_requests.items():
        # Only producer type: Add conversion back to default activation type
        if (type_req.producer_type is not None) and not type_req.consumers:
            tensor_quant_overrides[tensor_name][0]["convert"] = {"quant_type": activation_type}
        # Only consumers
        elif type_req.producer_type is None:
            prod_type = _get_output_activation_qtype(tensor_name, tensor_quant_overrides, activation_type)
            consumer_type = type_req.consumers[0]

            if prod_type != consumer_type:
                _add_to_convert_recv_nodes(
                    tensor_quant_overrides, tensor_name, prod_type, consumer_type, type_req.consumers[1]
                )
            else:
                if not _check_not_in_convert_recv_nodes(tensor_quant_overrides, tensor_name, type_req.consumers[1]):
                    raise ValueError(
                        "Tensor override for '{tensor_name}' converts the type for consumers that need the original type."
                    )
        # Both producer and consumers
        else:
            prod_type = type_req.producer_type
            consumer_type = type_req.consumers[0]

            if prod_type != consumer_type:
                _add_to_convert_recv_nodes(
                    tensor_quant_overrides, tensor_name, prod_type, consumer_type, type_req.consumers[1]
                )
            else:
                all_consumers = set([node.name for node in consumers[tensor_name]])
                consumers_for_original_type = all_consumers.difference(type_req.consumers[1])

                if len(consumers_for_original_type) == 0:
                    # All consumers want the overridden type, so no need for convert nodes!
                    assert "convert" not in tensor_quant_overrides[tensor_name][0]
                else:
                    # Some consumers don't want the overridden type.
                    _add_to_convert_recv_nodes(
                        tensor_quant_overrides, tensor_name, prod_type, activation_type, consumers_for_original_type
                    )


def get_qnn_qdq_config(
    model_input: Path,
    calibration_data_reader: CalibrationDataReader,
    calibrate_method=CalibrationMethod.MinMax,
    activation_type=QuantType.QUInt8,
    weight_type=QuantType.QUInt8,
    init_overrides=None,
    add_qtype_converts=True,
    per_channel=False,
):
    if per_channel:
        raise ValueError("QNN EP does not yet support per-channel quantization.")

    # Process model nodes to setup overrides.
    model = onnx.load_model(model_input)
    op_types = set()
    consumers = {}
    producers = {}
    name_to_initializer = {initializer.name: initializer for initializer in model.graph.initializer}
    value_infos = {vi.name: vi for vi in model.graph.value_info}
    value_infos.update({ot.name: ot for ot in model.graph.output})
    value_infos.update({it.name: it for it in model.graph.input})

    # Get map of output names -> node producer
    # Get map of tensor names -> consumers
    for node in model.graph.node:
        op_types.add(node.op_type)

        for input_name in node.input:
            if input_name:
                if input_name not in consumers:
                    consumers[input_name] = []

                consumers[input_name].append(node)

        for output_name in node.output:
            assert bool(output_name), "Node output name cannot be empty"
            assert output_name not in producers, "Tensor can only be generated by a single node"
            producers[output_name] = node

    tensor_quant_overrides = copy.deepcopy(init_overrides) if init_overrides else {}

    if tensor_quant_overrides and add_qtype_converts:
        _add_qtype_converts(
            tensor_quant_overrides, activation_type, value_infos, name_to_initializer, producers, consumers
        )

    for node in model.graph.node:
        if node.op_type == "MatMul" and weight_type in Q8_TYPES:
            weight_symmetric = weight_type == QuantType.QInt8

            input_16bit_act = None
            input_wgt = None

            for input_name in node.input:
                if input_name not in name_to_initializer:
                    qtype = _get_input_activation_qtype(tensor_quant_overrides, input_name, node.name, activation_type)
                    if qtype in Q16_TYPES:
                        input_16bit_act = input_name
                else:
                    input_wgt = input_name

            # Override initializer to use the weight_type
            if input_16bit_act and input_wgt:
                _set_weight_type(tensor_quant_overrides, input_wgt, weight_type, weight_symmetric)
        elif node.op_type == "LayerNormalization" and weight_type in Q8_TYPES:
            weight_symmetric = weight_type == QuantType.QInt8

            has_q16_activation = False
            for input_name in node.input:
                if input_name not in name_to_initializer:
                    qtype = _get_input_activation_qtype(tensor_quant_overrides, input_name, node.name, activation_type)
                    if qtype in Q16_TYPES:
                        has_q16_activation = True
                        break

            # Override initializers to use the weight_type. Don't override the bias input.
            if has_q16_activation:
                for i in range(2):
                    input_name = node.input[i]
                    if input_name in name_to_initializer:
                        _set_weight_type(tensor_quant_overrides, input_name, weight_type, weight_symmetric)
        elif node.op_type == "Sigmoid":
            output_type = _get_output_activation_qtype(node.output[0], tensor_quant_overrides, activation_type)

            if output_type == QuantType.QUInt16:
                _set_scale_zero_point(
                    tensor_quant_overrides,
                    node.output[0],
                    output_type,
                    np.array(1.0 / 65536.0, dtype=np.float32),
                    np.array(0, dtype=np.uint16),
                )
            elif output_type == QuantType.QInt16:
                _set_scale_zero_point(
                    tensor_quant_overrides,
                    node.output[0],
                    output_type,
                    np.array(1.0 / 32768.0, dtype=np.float32),
                    np.array(0, dtype=np.int16),
                )
        elif node.op_type == "Tanh":
            output_type = _get_output_activation_qtype(node.output[0], tensor_quant_overrides, activation_type)

            if output_type == QuantType.QUInt16:
                _set_scale_zero_point(
                    tensor_quant_overrides,
                    node.output[0],
                    output_type,
                    np.array(1.0 / 32768.0, dtype=np.float32),
                    np.array(32768, dtype=np.uint16),
                )
            elif output_type == QuantType.QInt16:
                _set_scale_zero_point(
                    tensor_quant_overrides,
                    node.output[0],
                    output_type,
                    np.array(1.0 / 32768.0, dtype=np.float32),
                    np.array(0, dtype=np.int16),
                )

    extra_options = {
        "MinimumRealRange": 0.0001,
        "DedicatedQDQPair": False,  # Let ORT optimizer duplicate DQ nodes
        "TensorQuantOverrides": tensor_quant_overrides,
    }

    # TODO: Remove this extra option once ORT uses an ONNX version that supports 16-bit Q/DQ ops.
    if activation_type in Q16_TYPES or weight_type in Q16_TYPES:
        extra_options["UseQDQContribOps"] = True

    return StaticQuantConfig(
        calibration_data_reader,
        calibrate_method=calibrate_method,
        activation_type=activation_type,
        weight_type=weight_type,
        op_types_to_quantize=list(op_types.difference(OP_TYPES_TO_EXCLUDE)),
        extra_options=extra_options,
    )