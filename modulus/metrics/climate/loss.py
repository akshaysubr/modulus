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

import numpy as np
import torch
from torch.nn import functional as F


class MSE_SSIM(torch.nn.Module):
    """
    This class provides a compound loss formulation combining differential structural similarity (SSIM) and mean squared
    error (MSE). Calling this class will compute the loss using SSIM for fields indicated by model attributes
    (model.ssim_fields).
    """

    def __init__(
        self,
        mse_params=None,
        ssim_params=None,
        ssim_variables=["ttr1h", "tcwv0"],
        weights=[0.5, 0.5],
    ):
        """
        Constructor method.

        Parameters:
        ----------
        mse_params: optional
            parameters to pass to MSE constructor
        ssim_params: optional
            dictionary of parameters to pass to SSIM constructor
        ssim variables: list, optional
            list of variables over which loss will be calculated using DSSIM and MSE
        param weights: list, optional
            variables identified as requireing SSIM-loss calculation
            will have their loss calculated by a weighted average od the DSSIM metric and MSE.
            The weights of this weighted average are identified here. [MSE_weight, DSSIM_weight]
        """

        super(MSE_SSIM, self).__init__()
        if ssim_params is None:
            self.ssim = SSIM()
        else:
            self.ssim = SSIM(**ssim_params)
        if mse_params is None:
            self.mse = torch.nn.MSELoss()
        else:
            self.mse = torch.nn.MSELoss(**mse_params)
        if np.sum(weights) == 1:
            self.mse_dssim_weights = weights
        else:
            raise ValueError("Weights passed to MSE_SSIM loss must sum to 1")
        self.ssim_variables = ssim_variables

    def forward(
        self, prediction: torch.tensor, targets: torch.tensor, model: torch.nn.Module
    ):  # TODO(David): Pass only the necessary params instead of the entire model
        """
        Forward pass of the MSE_SSIM loss

        param prediction: torch.Tensor
            Predicted image of shape [B, T, C, F, H, W]
        param targets: torch.Tensor
            Ground truth image of shape [B, T, C, F, H, W]
        param model: torch.nn.Module
            model over which loss is being computed

        Returns
        -------
        torch.Tensor
            The structural similarity loss
        """

        # check tensor shapes to ensure proper computation of loss
        try:
            if prediction.shape[-1] != prediction.shape[-2]:
                raise AssertionError
            if prediction.shape[3] != 12:
                raise AssertionError
            if prediction.shape[2] != model.output_channels:
                raise AssertionError
            if not (
                (prediction.shape[1] == model.output_time_dim)
                or (
                    prediction.shape[1] == model.output_time_dim // model.input_time_dim
                )
            ):
                raise AssertionError
        except AssertionError:
            print(
                f"losses.MSE_SSIM: expected output shape [batchsize, {model.output_time_dim}, {model.output_channels}, [spatial dims]] got {prediction.shape}"
            )
            exit()

        # store the location of output and target tensors
        device = prediction.get_device()
        # initialize losses by var tensor that will store the variable wise loss
        loss_by_var = torch.empty([prediction.shape[2]], device=f"cuda:{device}")
        # initialize weights tensor that will allow for a weighted average of MSE and SSIM
        weights = torch.tensor(self.mse_dssim_weights, device=f"cuda:{device}")
        # calculate variable wise loss
        for i, v in enumerate(model.output_variables):
            # for logging purposes calculated DSIM and MSE for each variable
            var_mse = self.mse(
                prediction[:, :, i : i + 1, :, :, :], targets[:, :, i : i + 1, :, :, :]
            )  # the slice operation here ensures the singleton dimension is not squashed
            var_dssim = torch.min(torch.tensor([1.0, float(var_mse)])) * (
                1
                - self.ssim(
                    prediction[:, :, i : i + 1, :, :, :],
                    targets[:, :, i : i + 1, :, :, :],
                )
            )
            if v in self.ssim_variables:
                # compute weighted average between mse and dssim
                loss_by_var[i] = torch.sum(weights * torch.stack([var_mse, var_dssim]))
            else:
                loss_by_var[i] = var_mse
            model.log(
                f"MSEs_train/{model.output_variables[i]}",
                var_mse,
                batch_size=model.batch_size,
            )
            model.log(
                f"DSIMs_train/{model.output_variables[i]}",
                var_dssim,
                batch_size=model.batch_size,
            )
            model.log(
                f"losses_train/{model.output_variables[i]}",
                loss_by_var[i],
                batch_size=model.batch_size,
            )
        loss = loss_by_var.mean()
        model.log("losses_train/all_vars", loss, batch_size=model.batch_size)
        return loss


class SSIM(torch.nn.Module):
    """
    This class provides a differential structural similarity (SSIM) as loss for training an artificial neural network. The
    advantage of SSIM over the conventional mean squared error is a relation to images where SSIM incorporates the local
    neighborhood when determining the quality of an individual pixel. Results are less blurry, as demonstrated here
    https://ece.uwaterloo.ca/~z70wang/research/ssim/

    Code is origininally taken from https://github.com/Po-Hsun-Su/pytorch-ssim
    Modifications include comments and an optional training phase with the mean squared error (MSE) preceding the SSIM
    loss, to bring the weights on track. Otherwise, SSIM often gets stuck early in a local minimum.
    """

    def __init__(
        self,
        window_size: int = 11,
        time_series_forecasting: bool = False,
        padding_mode: str = "constant",
        mse: bool = False,
        mse_epochs: int = 0,
    ):
        """
        Constructor method.

        param window_size: int, optional
            The patch size over which the SSIM is computed
        param time_series_forecasting: bool ,optional
            Boolean indicating whether time series forecasting is the task
        param padding_mode: str
            Padding mode used for padding input images, e.g. 'zeros', 'replicate', 'reflection'
        param mse: torch.nn.Module
            Uses MSE parallel
        param mse_epochs: int, optional
            Number of MSE epochs preceding the SSIM epochs during training
        """
        super(SSIM, self).__init__()
        self.window_size = window_size
        self.time_series_forecasting = time_series_forecasting
        self.padding_mode = padding_mode
        self.mse = torch.nn.MSELoss() if mse else None
        self.mse_epochs = mse_epochs
        self.c1, self.c2 = 0.01**2, 0.03**2

        self.register_buffer(
            "window", self._create_window(window_size), persistent=False
        )

    def forward(
        self,
        predicted: torch.Tensor,
        target: torch.Tensor,
        mask: torch.Tensor = None,
        epoch: int = 0,
    ) -> torch.Tensor:
        """
        Forward pass of the SSIM loss

        param predicted: torch.Tensor
            Predicted image of shape [B, T, C, F, H, W]
        param target: torch.Tensor
            Ground truth image of shape [B, T, C, F, H, W]
        param mask: torch.Tensor, optional
            Mask for excluding pixels
        param epoch: int, optional
            The current epoch

        Returns
        -------
        torch.Tensor
            The structural similarity loss
        """
        predicted = predicted.transpose(dim0=2, dim1=3)
        target = target.transpose(dim0=2, dim1=3)
        if self.time_series_forecasting:
            # Join Batch and time dimension
            predicted = torch.flatten(predicted, start_dim=0, end_dim=2)
            target = torch.flatten(target, start_dim=0, end_dim=2)

        window = self.window.expand(predicted.shape[1], -1, -1, -1)

        if window.dtype != predicted.dtype:
            window = window.to(dtype=predicted.dtype)

        return self._ssim(predicted, target, window, mask, epoch)

    @staticmethod
    def _gaussian(window_size: int, sigma: float) -> torch.Tensor:
        """
        Computes a Gaussian over the size of the window to weigh distant pixels less.

        :param window_size: The size of the patches
        :param sigma: The width of the Gaussian curve
        :return: A tensor representing the weights for each pixel in the window or patch
        """
        x = torch.arange(0, window_size) - window_size // 2
        gauss = torch.exp(-((x.div(2 * sigma)) ** 2))
        return gauss / gauss.sum()

    def _create_window(self, window_size: int, sigma: float = 1.5) -> torch.Tensor:
        """
        Creates the weights of the window or patches.

        :param window_size: The size of the patches
        :param sigma: The width of the Gaussian curve
        """
        _1D_window = self._gaussian(window_size, sigma).unsqueeze(-1)
        _2D_window = _1D_window.mm(_1D_window.t()).unsqueeze(0).unsqueeze(0)
        return _2D_window

    def _ssim(
        self,
        predicted: torch.Tensor,
        target: torch.Tensor,
        window: torch.Tensor,
        mask: torch.Tensor = None,
        epoch: int = 0,
    ) -> torch.Tensor:
        """
        Computes the SSIM loss between two image tensors

        :param _predicted: The predicted image tensor
        :param _target: The target image tensor
        :param window: The weights for each pixel in the window over which the SSIM is computed
        :param mask: Mask for excluding pixels
        :param epoch: The current epoch
        :return: The SSIM between predicted and target
        """
        if epoch < self.mse_epochs:
            # If specified, the MSE is used for the first self.mse_epochs epochs
            return F.mse_loss(predicted, target)

        channels = window.shape[0]
        window_size = window.shape[2]

        window = window.to(device=predicted.device)

        _predicted = F.pad(
            predicted,
            pad=[
                (window_size - 1) // 2,
                (window_size - 1) // 2 + (window_size - 1) % 2,
                (window_size - 1) // 2,
                (window_size - 1) // 2 + (window_size - 1) % 2,
            ],
            mode=self.padding_mode,
        )

        _target = F.pad(
            target,
            pad=[
                (window_size - 1) // 2,
                (window_size - 1) // 2 + (window_size - 1) % 2,
                (window_size - 1) // 2,
                (window_size - 1) // 2 + (window_size - 1) % 2,
            ],
            mode=self.padding_mode,
        )

        mu1 = F.conv2d(_predicted, window, padding=0, groups=channels)
        mu2 = F.conv2d(_target, window, padding=0, groups=channels)

        mu1_sq = mu1.pow(2)
        mu2_sq = mu2.pow(2)
        mu1_mu2 = mu1 * mu2

        sigma1_sq = (
            F.conv2d(_predicted * _predicted, window, padding=0, groups=channels)
            - mu1_sq
        )
        sigma2_sq = (
            F.conv2d(_target * _target, window, padding=0, groups=channels) - mu2_sq
        )
        sigma12_sq = (
            F.conv2d(_predicted * _target, window, padding=0, groups=channels) - mu1_mu2
        )

        ssim_map = ((2 * mu1_mu2 + self.c1) * (2 * sigma12_sq + self.c2)) / (
            (mu1_sq + mu2_sq + self.c1) * (sigma1_sq + sigma2_sq + self.c2)
        )

        if mask is not None:
            ssim_map = ssim_map[..., mask]
            predicted = predicted[..., mask]
            target = target[..., mask]

        ssim = ssim_map.mean().abs()

        if self.mse:
            ssim = ssim + self.mse(predicted, target)

        return ssim
