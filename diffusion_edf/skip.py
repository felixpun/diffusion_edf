from typing import List, Optional, Union, Tuple
import math

import torch
from e3nn import o3
from e3nn.util.jit import compile_mode

from diffusion_edf.equiformer.tensor_product_rescale import LinearRS
from diffusion_edf.equiformer.layer_norm import EquivariantLayerNormV2


#@compile_mode('script')
class ProjectIfMismatch(torch.nn.Module):
    def __init__(self, irreps_in: o3.Irreps, irreps_out: o3.Irreps):
        super().__init__()
        self.irreps_in = o3.Irreps(irreps_in)
        self.irreps_out = o3.Irreps(irreps_out)
        if self.irreps_in == self.irreps_out:
            self.skip = torch.nn.Identity()
            self.layernorm = torch.nn.Identity()
        else:
            self.skip = LinearRS(irreps_in=self.irreps_in,
                                 irreps_out=self.irreps_out,
                                 bias=True,
                                 rescale=True)
            self.layernorm = EquivariantLayerNormV2(self.irreps_out)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.skip(x)
        x = self.layernorm(x)
        return x