import torch
import torch.nn as nn
import torch.ao.nn.intrinsic as nni
import torch.nn.functional as F
import torch.nn.quantized._reference as nnqr
from ._common_operator_config_utils import (
    _get_conv_configs,
    _get_linear_configs,
    _get_binary_op_configs,
    _get_bn_configs,
    _get_cat_config,
    _get_default_op_configs,
    _get_embedding_op_configs,
    _get_fixed_qparams_op_configs,
    _get_ln_configs,
    _get_rnn_op_configs,
    _get_share_qparams_op_configs,
)
from .backend_config import (
    BackendPatternConfig,
    BackendConfig,
    DTypeConfig,
    ObservationType,
)
from ..fuser_method_mappings import (
    _sequential_wrapper2,
)
import operator
from torch.ao.quantization.utils import MatchAllNode

# ===================
# |  DTYPE CONFIGS  |
# ===================

onednn_weighted_op_int8_dtype_config = DTypeConfig(
    input_dtype=torch.quint8,
    output_dtype=torch.quint8,
    weight_dtype=torch.qint8,
    bias_dtype=torch.float,
)

onednn_op_quint8_dtype_config = DTypeConfig(
    input_dtype=torch.quint8,
    output_dtype=torch.quint8,
)

onednn_dynamic_int8_dtype_config = DTypeConfig(
    input_dtype=torch.quint8,
    output_dtype=torch.float,
    weight_dtype=torch.qint8,
    bias_dtype=torch.float,
    is_dynamic=True,
)

onednn_weight_only_qint8_dtype_config = DTypeConfig(
    input_dtype=torch.float,
    output_dtype=torch.float,
    weight_dtype=torch.qint8,
)

onednn_input_output_only_quint8_dtype_config = DTypeConfig(
    input_dtype=torch.quint8,
    output_dtype=torch.quint8,
    weight_dtype=torch.float,
    bias_dtype=torch.float,
)

# ===================
# |  FUSER METHODS  |
# ===================

def _fuse_linear_bn_leaky_relu(is_qat, linear, bn, leaky_relu):
    r"""Given the linear, bn and leaky_relu modules, fuses them and returns the fused module
    Args:
        is_qat: a flag for whether we are using quantization aware training fusion
                or post training quantization fusion
        linear: Module instance of type Linear
        bn: BatchNorm1d instance that needs to be fused with the linear layer
        leaky_relu: LeakyReLU instance that needs to be fused with the linear layer
    Examples::
        >>> m1 = nn.Linear(20, 10)
        >>> b1 = nn.BatchNorm1d(10)
        >>> lr = nn.LeakyReLU(0.01)
        >>> m2 = _fuse_linear_bn_leaky_relu(m1, b1, lr)
    """
    assert(linear.training == bn.training and bn.training == leaky_relu.training),\
        "Linear, BN and LeakyReLU all must be in the same mode (train or eval)."

    if is_qat:
        raise NotImplementedError("Cannot fuse train modules: {}".format((linear, bn, leaky_relu)))
    else:
        map_to_fused_module_eval = {
            nn.Linear: nni.LinearLeakyReLU,
        }
        fused_module = map_to_fused_module_eval.get(type(linear), None)
        if fused_module is not None:
            fused_linear = nn.utils.fusion.fuse_linear_bn_eval(linear, bn)
            fm = fused_module(fused_linear, leaky_relu)
            return fm
        else:
            raise NotImplementedError("Cannot fuse eval modules: {}".format((linear, bn, leaky_relu)))

# ======================
# |  CONFIGS FOR CONV  |
# ======================
observation_type = ObservationType.OUTPUT_USE_DIFFERENT_OBSERVER_AS_INPUT

conv_dtype_configs = [onednn_weighted_op_int8_dtype_config]
conv_configs = _get_conv_configs(conv_dtype_configs)

# (1) Conv2d + Add
# conv2d   Y
#   \   /
#    add

# include:
# conv2d conv2d
#   \   /
#    add

def fuse_conv_add_right(is_qat, add, conv, _):
    return nni.ConvAdd2d(add, conv)

def conv_add_root_node_getter_right(pattern):
    _, conv, _ = pattern
    return conv

def conv_add_extra_inputs_getter_right(pattern):
    """ get inputs pattern for extra inputs, inputs for root node
    are assumed to be copied over from root node to the fused node
    """
    _, conv, extra_input = pattern
    return [extra_input]

for add_op in [torch.add, operator.add]:
    conv_configs.append(
        BackendPatternConfig()
            ._set_pattern_complex_format((add_op, nn.Conv2d, MatchAllNode))  # noqa: E131
            .set_observation_type(observation_type)
            .set_dtype_configs(conv_dtype_configs)
            .set_fuser_method(fuse_conv_add_right)
            ._set_root_node_getter(conv_add_root_node_getter_right)
            ._set_extra_inputs_getter(conv_add_extra_inputs_getter_right)
            .set_fused_module(nni.ConvAdd2d))

#  Y    conv
#   \   /
#    add
def fuse_conv_add(is_qat, add, _, conv):
    return nni.ConvAdd2d(add, conv)

def conv_add_root_node_getter(pattern):
    add, _, conv = pattern
    return conv

def conv_add_extra_inputs_getter(pattern):
    """ get inputs pattern for extra inputs, inputs for root node
    are assumed to be copied over from root node to the fused node
    """
    _, extra_input, conv = pattern
    return [extra_input]

for add_op in [torch.add, operator.add]:
    conv_configs.append(
        BackendPatternConfig()
            ._set_pattern_complex_format((add_op, MatchAllNode, nn.Conv2d))  # noqa: E131
            .set_observation_type(observation_type)
            .set_dtype_configs(conv_dtype_configs)
            .set_fuser_method(fuse_conv_add)
            ._set_root_node_getter(conv_add_root_node_getter)
            ._set_extra_inputs_getter(conv_add_extra_inputs_getter)
            .set_fused_module(nni.ConvAdd2d))

conv_configs.append(
    BackendPatternConfig(nni.ConvAdd2d)
        .set_observation_type(observation_type)  # noqa: E131
        .set_dtype_configs(conv_dtype_configs)
        .set_root_module(nn.Conv2d)
        .set_reference_quantized_module(nnqr.Conv2d))

# ========================
# |  CONFIGS FOR LINEAR  |
# ========================

linear_dtype_configs = [
    onednn_weighted_op_int8_dtype_config,
    onednn_dynamic_int8_dtype_config,
]
linear_configs = _get_linear_configs(linear_dtype_configs)

# (1) Linear + leaky_relu
# -------------------
# 1.1 linear module + leaky_relu fusion config
# linear leaky_relu, linear module + leaky_relu module
linear_configs.append(
    BackendPatternConfig((nn.Linear, nn.LeakyReLU))
        .set_dtype_configs(linear_dtype_configs)  # noqa: E131
        .set_fuser_method(_sequential_wrapper2(nni.LinearLeakyReLU))
        .set_fused_module(nni.LinearLeakyReLU))
# linear leaky_relu, linear module + functional leaky_relu
linear_configs.append(
    BackendPatternConfig((nn.Linear, F.leaky_relu))
        .set_dtype_configs(linear_dtype_configs)  # noqa: E131
        .set_fuser_method(_sequential_wrapper2(nni.LinearLeakyReLU))
        .set_fused_module(nni.LinearLeakyReLU))
# linear leaky_relu, linear module + BN + leaky_relu
linear_configs.append(
    BackendPatternConfig((nn.Linear, nn.BatchNorm1d, nn.LeakyReLU))
        .set_dtype_configs(linear_dtype_configs)  # noqa: E131
        .set_fuser_method(_fuse_linear_bn_leaky_relu)
        .set_fused_module(nni.LinearLeakyReLU))

# 1.2 linear module + leaky_relu, fused module configs
# linear leaky_relu, fused module
linear_configs.append(
    BackendPatternConfig(nni.LinearLeakyReLU)
        .set_observation_type(observation_type)  # noqa: E131
        .set_dtype_configs(linear_dtype_configs)
        .set_root_module(nn.Linear)
        .set_reference_quantized_module(nnqr.Linear))
# 1.3 functional linear + leaky_relu configs
# linear leaky_relu, functional linear + leaky_relu module
linear_configs.append(
    BackendPatternConfig((F.linear, nn.LeakyReLU))
        .set_observation_type(observation_type)  # noqa: E131
        .set_dtype_configs(linear_dtype_configs))
# linear leaky_relu, functional linear + functional leaky_relu
linear_configs.append(
    BackendPatternConfig((F.linear, F.leaky_relu))
        .set_observation_type(observation_type)  # noqa: E131
        .set_dtype_configs(linear_dtype_configs))

# ===========================
# |  CONFIGS FOR OTHER OPS  |
# ===========================

binary_op_dtype_configs = [onednn_op_quint8_dtype_config]
default_op_dtype_configs = [onednn_op_quint8_dtype_config]
fixed_qparams_op_dtype_configs = [onednn_op_quint8_dtype_config]
share_qparams_op_dtype_configs = [onednn_op_quint8_dtype_config]
rnn_op_dtype_configs = [onednn_dynamic_int8_dtype_config]
embedding_op_dtype_configs = [onednn_weight_only_qint8_dtype_config]
layer_norm_op_dtype_configs = [onednn_input_output_only_quint8_dtype_config]

# =====================
# |  BACKEND CONFIGS  |
# =====================

def get_onednn_backend_config() -> BackendConfig:
    """
    Return the `BackendConfig` for PyTorch's native ONEDNN backend.
    """
    return BackendConfig("onednn") \
        .set_backend_pattern_configs(conv_configs) \
        .set_backend_pattern_configs(linear_configs) \
        .set_backend_pattern_configs(_get_binary_op_configs(binary_op_dtype_configs)) \
        .set_backend_pattern_config(_get_cat_config(default_op_dtype_configs)) \
        .set_backend_pattern_configs(_get_default_op_configs(default_op_dtype_configs)) \
        .set_backend_pattern_configs(_get_fixed_qparams_op_configs(fixed_qparams_op_dtype_configs)) \
        .set_backend_pattern_configs(_get_share_qparams_op_configs(share_qparams_op_dtype_configs)) \
        .set_backend_pattern_configs(_get_bn_configs(default_op_dtype_configs)) \
        .set_backend_pattern_configs(_get_ln_configs(layer_norm_op_dtype_configs)) \
        .set_backend_pattern_configs(_get_rnn_op_configs(rnn_op_dtype_configs)) \
        .set_backend_pattern_configs(_get_embedding_op_configs(embedding_op_dtype_configs))

__all__ = [
    "get_onednn_backend_config",
]
