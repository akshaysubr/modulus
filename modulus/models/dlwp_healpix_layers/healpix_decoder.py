from typing import Any, Dict, Optional, Sequence, Union

from hydra.utils import instantiate
from omegaconf import DictConfig
import torch as th


class UNetDecoder(th.nn.Module):
    """
    Generic UNetDecoder that can be applied to arbitrary meshes.
    """
    def __init__(
            self,
            conv_block: DictConfig,
            up_sampling_block: DictConfig,
            output_layer: DictConfig,
            recurrent_block: DictConfig = None,
            n_channels: Sequence = (64, 32, 16),
            n_layers: Sequence = (1, 2, 2),
            output_channels: int = 1,
            dilations: list = None,
            enable_nhwc: bool = False,
            enable_healpixpad: bool = False
    ):
        super().__init__()
        self.channel_dim = 1  # 1 in previous layout

        if enable_nhwc and activation is not None:
            activation = activation.to(memory_format=th.channels_last)

        if dilations is None:
            # Defaults to [1, 1, 1...] in accordance with the number of unet levels
            dilations = [1 for _ in range(len(n_channels))]

        self.decoder = []
        for n, curr_channel in enumerate(n_channels):

            # Second half of the synoptic layer does not need an upsampling module
            if n == 0:
                up_sample_module = None
            else:
                up_sample_module = instantiate(
                    config=up_sampling_block,
                    in_channels=curr_channel,
                    out_channels=curr_channel,
                    enable_nhwc=enable_nhwc,
                    enable_healpixpad=enable_healpixpad
                    )

            next_channel = n_channels[n+1] if n < len(n_channels) - 1 else n_channels[-1]

            conv_module = instantiate(
                config=conv_block,
                in_channels=curr_channel*2 if n > 0 else curr_channel,  # Considering skip connection
                latent_channels=curr_channel,
                out_channels=next_channel,
                dilation=dilations[n],
                n_layers=n_layers[n],
                enable_nhwc=enable_nhwc,
                enable_healpixpad=enable_healpixpad
                )

            # Recurrent module
            if recurrent_block is not None:
                rec_module = instantiate(
                    config=recurrent_block,
                    in_channels=next_channel,
                    enable_healpixpad=enable_healpixpad
                    )
            else:
                rec_module = None

            self.decoder.append(th.nn.ModuleDict(
                {"upsamp": up_sample_module,
                 "conv": conv_module,
                 "recurrent": rec_module}
                ))

        self.decoder = th.nn.ModuleList(self.decoder)

        # (Linear) Output layer
        self.output_layer = instantiate(
            config=output_layer,
            in_channels=curr_channel,
            out_channels=output_channels,
            dilation=dilations[-1],
            enable_nhwc=enable_nhwc,
            enable_healpixpad=enable_healpixpad
            )

    def forward(self, inputs: Sequence) -> th.Tensor:
        x = inputs[-1]
        for n, layer in enumerate(self.decoder):
            if layer["upsamp"] is not None:
                up = layer["upsamp"](x)
                x = th.cat([up, inputs[-1 - n]], dim=self.channel_dim)
            x = layer["conv"](x)
            if layer["recurrent"] is not None: x = layer["recurrent"](x)
        return self.output_layer(x)

    def reset(self):
        for layer in self.decoder:
            layer["recurrent"].reset()

