# Copyright (c) 2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
import warnings

import torch
import torch.nn as nn
import torch.nn.functional as F

from modulus.distributed.manager import DistributedManager
from modulus.distributed.mappings import (
    copy_to_parallel_region,
    gather_from_parallel_region,
    reduce_from_parallel_region,
    scatter_to_parallel_region,
)


def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    # Cut & paste from PyTorch official master until it's in a few official releases
    # Method based on
    # https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn(
            "mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
            "The distribution of values may be incorrect.",
            stacklevel=2,
        )

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        low = norm_cdf((a - mean) / std)
        up = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [low, up], then translate to
        # [2low-1, 2up-1].
        tensor.uniform_(2 * low - 1, 2 * up - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.0))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor


def trunc_normal_(tensor, mean=0.0, std=1.0, a=-2.0, b=2.0):
    r"""Fills the input Tensor with values drawn from a truncated
    normal distribution. The values are effectively drawn from the
    normal distribution :math:`\mathcal{N}(\text{mean}, \text{std}^2)`
    with values outside :math:`[a, b]` redrawn until they are within
    the bounds. The method used for generating the random values works
    best when :math:`a \leq \text{mean} \leq b`.
    Args:
    tensor: an n-dimensional `torch.Tensor`
    mean: the mean of the normal distribution
    std: the standard deviation of the normal distribution
    a: the minimum cutoff value
    b: the maximum cutoff value
    Examples:
    >>> w = torch.empty(3, 5)
    >>> o = nn.init.trunc_normal_(w)
    """
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)


@torch.jit.script
def drop_path(
    x: torch.Tensor, drop_prob: float = 0.0, training: bool = False
) -> torch.Tensor:
    """
    Drop paths (Stochastic Depth) per sample (when applied in main path of
    residual blocks).
    This is the same as the DropConnect implfor EfficientNet, etc networks, however,
    the original name is misleading as 'Drop Connect' is a different form of dropout in
    a separate paper.
    See discussion: https://github.com/tensorflow/tpu/issues/494#issuecomment-532968956
    Opted for changing the layer and argument names to 'drop path' rather than mix
    DropConnect as a layer name and use 'survival rate' as the argument.
    """
    if drop_prob == 0.0 or not training:
        return x
    keep_prob = 1.0 - drop_prob
    shape = (x.shape[0],) + (1,) * (
        x.ndim - 1
    )  # work with diff dim tensors, not just 2D ConvNets
    random_tensor = keep_prob + torch.rand(shape, dtype=x.dtype, device=x.device)
    random_tensor.floor_()  # binarize
    output = x.div(keep_prob) * random_tensor
    return output


class DropPath(nn.Module):
    """
    Drop paths (Stochastic Depth) per sample (when applied in main path of
    residual blocks).
    """

    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)


class DistributedMLP(nn.Module):
    def __init__(
        self,
        in_features,
        hidden_features=None,
        out_features=None,
        act_layer=nn.GELU,
        drop=0.0,
        input_is_matmul_parallel=False,
        output_is_matmul_parallel=False,
    ):
        super(DistributedMLP, self).__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.input_is_matmul_parallel = input_is_matmul_parallel
        self.output_is_matmul_parallel = output_is_matmul_parallel

        # get effective embedding size:
        comm_size = DistributedManager().group_size("model_parallel")
        if not (hidden_features % comm_size == 0):
            raise ValueError(
                "Error, hidden_features needs to be divisible by matmul_parallel_size"
            )
        hidden_features_local = hidden_features // comm_size

        # first set of hp
        self.w1 = nn.Parameter(torch.ones(hidden_features_local, in_features, 1, 1))
        self.b1 = nn.Parameter(torch.zeros(hidden_features_local))

        # second set of hp
        self.w2 = nn.Parameter(torch.ones(out_features, hidden_features_local, 1, 1))
        self.b2 = nn.Parameter(torch.zeros(out_features))

        self.act = act_layer()
        self.drop = nn.Dropout(drop) if drop > 0.0 else nn.Identity()

        # init weights
        self._init_weights()

    def _init_weights(self):
        trunc_normal_(self.w1, std=0.02)
        nn.init.constant_(self.b1, 0.0)
        trunc_normal_(self.w2, std=0.02)
        nn.init.constant_(self.b2, 0.0)

    def forward(self, x):
        # gather if input is MP
        if self.input_is_matmul_parallel:
            x = gather_from_parallel_region(x, dim=1, group="model_parallel")

        x = copy_to_parallel_region(x, group="model_parallel")
        x = F.conv2d(x, self.w1, bias=self.b1)
        x = self.act(x)
        x = self.drop(x)
        x = F.conv2d(x, self.w2, bias=None)
        x = reduce_from_parallel_region(x, group="model_parallel")
        x = x + torch.reshape(self.b2, (1, -1, 1, 1))
        x = self.drop(x)

        # scatter if output is MP
        if self.output_is_matmul_parallel:
            x = scatter_to_parallel_region(x, dim=1, group="model_parallel")

        return x


class DistributedPatchEmbed(nn.Module):
    def __init__(
        self,
        inp_shape=(224, 224),
        patch_size=(16, 16),
        in_chans=3,
        embed_dim=768,
        input_is_matmul_parallel=False,
        output_is_matmul_parallel=True,
    ):
        super(DistributedPatchEmbed, self).__init__()

        # store params
        self.input_parallel = input_is_matmul_parallel
        self.output_parallel = output_is_matmul_parallel

        # get comm sizes:
        matmul_comm_size = DistributedManager().group_size("model_parallel")

        # compute parameters
        num_patches = (inp_shape[1] // patch_size[1]) * (inp_shape[0] // patch_size[0])
        self.inp_shape = (inp_shape[0], inp_shape[1])
        self.patch_size = patch_size
        self.num_patches = num_patches

        if self.input_parallel:
            if not (in_chans % matmul_comm_size == 0):
                raise ValueError(
                    "Error, the in_chans needs to be divisible by matmul_parallel_size"
                )

        # get effective embedding size:
        if self.output_parallel:
            if not (embed_dim % matmul_comm_size == 0):
                raise ValueError(
                    "Error, the embed_dim needs to be divisible by matmul_parallel_size"
                )
            out_chans_local = embed_dim // matmul_comm_size
        else:
            out_chans_local = embed_dim

        # the weights  of this layer is shared across spatial parallel ranks
        self.proj = nn.Conv2d(
            in_chans, out_chans_local, kernel_size=patch_size, stride=patch_size
        )

        # make sure we reduce them across rank
        self.proj.weight.is_shared_spatial = True
        self.proj.bias.is_shared_spatial = True

    def forward(self, x):
        if self.input_parallel:
            x = gather_from_parallel_region(x, dim=1, group="model_parallel")

        if self.output_parallel:
            x = copy_to_parallel_region(x, group="model_parallel")

        B, C, H, W = x.shape
        if not (H == self.inp_shape[0] and W == self.inp_shape[1]):
            raise ValueError(
                f"Input input size ({H}*{W}) doesn't match model ({self.inp_shape[0]}*{self.inp_shape[1]})."
            )
        # new: B, C, H*W
        x = self.proj(x).flatten(2)
        return x


@torch.jit.script
def compl_mul_add_fwd(
    a: torch.Tensor, b: torch.Tensor, c: torch.Tensor
) -> torch.Tensor:
    tmp = torch.einsum("bkixys,kiot->stbkoxy", a, b)
    res = (
        torch.stack(
            [tmp[0, 0, ...] - tmp[1, 1, ...], tmp[1, 0, ...] + tmp[0, 1, ...]], dim=-1
        )
        + c
    )
    return res


@torch.jit.script
def compl_mul_add_fwd_c(
    a: torch.Tensor, b: torch.Tensor, c: torch.Tensor
) -> torch.Tensor:
    ac = torch.view_as_complex(a)
    bc = torch.view_as_complex(b)
    cc = torch.view_as_complex(c)
    tmp = torch.einsum("bkixy,kio->bkoxy", ac, bc)
    res = tmp + cc
    return torch.view_as_real(res)


class DistributedAFNO2D(nn.Module):
    def __init__(
        self,
        hidden_size,
        num_blocks=8,
        sparsity_threshold=0.01,
        hard_thresholding_fraction=1,
        hidden_size_factor=1,
        input_is_matmul_parallel=False,
        output_is_matmul_parallel=False,
    ):
        super(DistributedAFNO2D, self).__init__()
        if not (hidden_size % num_blocks == 0):
            raise ValueError(
                f"hidden_size {hidden_size} should be divisible by num_blocks {num_blocks}"
            )

        # get comm sizes:
        matmul_comm_size = DistributedManager().group_size("model_parallel")

        self.fft_handle = torch.fft.rfft2
        self.ifft_handle = torch.fft.irfft2

        self.hidden_size = hidden_size
        self.sparsity_threshold = sparsity_threshold
        self.num_blocks = num_blocks
        if not (self.num_blocks % matmul_comm_size == 0):
            raise ValueError(
                "Error, num_blocks needs to be divisible by matmul_parallel_size"
            )
        self.num_blocks_local = self.num_blocks // matmul_comm_size
        self.block_size = self.hidden_size // self.num_blocks
        self.hard_thresholding_fraction = hard_thresholding_fraction
        self.hidden_size_factor = hidden_size_factor
        self.scale = 0.02
        use_complex_mult = False
        self.mult_handle = (
            compl_mul_add_fwd_c if use_complex_mult else compl_mul_add_fwd
        )

        # model parallelism
        self.input_is_matmul_parallel = input_is_matmul_parallel
        self.output_is_matmul_parallel = output_is_matmul_parallel

        # new
        # these weights need to be synced across all spatial ranks!
        self.w1 = nn.Parameter(
            self.scale
            * torch.randn(
                self.num_blocks_local,
                self.block_size,
                self.block_size * self.hidden_size_factor,
                2,
            )
        )
        self.b1 = nn.Parameter(
            self.scale
            * torch.randn(
                self.num_blocks_local,
                self.block_size * self.hidden_size_factor,
                1,
                1,
                2,
            )
        )
        self.w2 = nn.Parameter(
            self.scale
            * torch.randn(
                self.num_blocks_local,
                self.block_size * self.hidden_size_factor,
                self.block_size,
                2,
            )
        )
        self.b2 = nn.Parameter(
            self.scale * torch.randn(self.num_blocks_local, self.block_size, 1, 1, 2)
        )

        # make sure we reduce them across rank
        self.w1.is_shared_spatial = True
        self.b1.is_shared_spatial = True
        self.w2.is_shared_spatial = True
        self.b2.is_shared_spatial = True

    def forward(self, x):
        if not self.input_is_matmul_parallel:
            # distribute data
            x = scatter_to_parallel_region(x, dim=1, group="model_parallel")

        # bias
        bias = x

        dtype = x.dtype
        x = x.float()
        B, C, H, W = x.shape
        total_modes = H // 2 + 1
        kept_modes = int(total_modes * self.hard_thresholding_fraction)

        x = self.fft_handle(x, (H, W), (-2, -1), "ortho")
        x = x.view(B, self.num_blocks_local, self.block_size, H, W // 2 + 1)

        # new
        x = torch.view_as_real(x)
        o2 = torch.zeros(x.shape, device=x.device)

        o1 = F.relu(
            self.mult_handle(
                x[
                    :,
                    :,
                    :,
                    total_modes - kept_modes : total_modes + kept_modes,
                    :kept_modes,
                    :,
                ],
                self.w1,
                self.b1,
            )
        )
        o2[
            :, :, :, total_modes - kept_modes : total_modes + kept_modes, :kept_modes, :
        ] = self.mult_handle(o1, self.w2, self.b2)

        # finalize
        x = F.softshrink(o2, lambd=self.sparsity_threshold)
        x = torch.view_as_complex(x)
        x = x.reshape(B, C, H, W // 2 + 1)
        x = self.ifft_handle(x, (H, W), (-2, -1), "ortho")
        x = x.type(dtype) + bias

        # gather
        if not self.output_is_matmul_parallel:
            x = gather_from_parallel_region(x, dim=1, group="model_parallel")

        return x
