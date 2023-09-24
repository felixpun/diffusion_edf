import os
from typing import Tuple
import torch

_Jd, _W3j_flat, _W3j_indices = torch.load(os.path.join(os.path.dirname(__file__), 'constants.pt'))
_Jd: Tuple[torch.Tensor] = tuple(J.detach().clone().to(dtype=torch.float32) for J in _Jd)