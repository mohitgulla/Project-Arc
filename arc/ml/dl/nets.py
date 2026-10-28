"""Torch networks of the E27.6 challengers (``torch`` = the optional ``ml-dl`` extra).

Every challenger ends in the same multi-task head (:data:`HEADS`), so its outputs map
one-to-one onto M2's (:data:`arc.ml.m2.predict.OUTPUTS`): ``win`` / ``stop`` logits,
the Huber ``r`` regressor, the three ``r`` quantiles, ``days`` and the conditional
``r_win`` / ``r_loss`` regressors behind ``e_r_mix``.

* :class:`ResMLP`: Gorishniy et al. (2021) ResNet: linear stem, ``n_blocks`` ×
  (LayerNorm → Linear(d, 2d) → ReLU → Dropout → Linear(2d, d) → Dropout) + residual.
* :class:`FTTransformer`: one token per numeric feature (``z·W_j + miss·M_j + b_j``)
  and per category (embedding), a CLS token, ``ft_layers`` pre-norm transformer
  layers; the CLS output feeds the head.
* :class:`SequenceEncoder`: dilated 1-D CNN over the (window × channel) per-ticker
  daily vectors; mean-pooled and last-step features → ``d_aux``.
* :class:`SurfaceEncoder`: two 3×3 conv layers over the (channel × Δ × DTE) IV grid
  → ``d_aux``.

An aux embedding is concatenated with the tabular input of the ResMLP trunk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from arc.ml.dl.config import NetSpec

HEADS: tuple[str, ...] = ("win", "stop", "r", "q10", "q50", "q90", "days", "r_win", "r_loss")
CAT_DIM = 8


def load_torch() -> Any:
    """``torch`` (``uv sync --extra ml-dl``)."""
    import torch

    return torch


def build_net(
    spec: NetSpec,
    *,
    n_num: int,
    cards: Sequence[int],
    aux_shape: tuple[int, ...] | None = None,
) -> Any:
    """The challenger's ``nn.Module`` (forward(z, miss, cat, aux) -> (B, len(HEADS)))."""
    torch = load_torch()
    nn = torch.nn

    class SequenceEncoder(nn.Module):
        def __init__(self, channels: int, d_out: int) -> None:
            super().__init__()
            h = 32
            self.net = nn.Sequential(
                nn.Conv1d(channels, h, 5, padding=2),
                nn.GELU(),
                nn.Conv1d(h, h, 5, padding=4, dilation=2),
                nn.GELU(),
                nn.Conv1d(h, h, 5, padding=8, dilation=4),
                nn.GELU(),
            )
            self.out = nn.Linear(2 * h, d_out)

        def forward(self, x: Any) -> Any:  # x: (B, T, C)
            y = self.net(x.transpose(1, 2))
            return self.out(torch.cat([y.mean(dim=2), y[:, :, -1]], dim=1))

    class SurfaceEncoder(nn.Module):
        def __init__(self, shape: tuple[int, ...], d_out: int) -> None:
            super().__init__()
            c, hgt, wid = shape
            self.net = nn.Sequential(
                nn.Conv2d(c, 16, 3, padding=1),
                nn.GELU(),
                nn.Conv2d(16, 32, 3, padding=1),
                nn.GELU(),
            )
            self.out = nn.Linear(32 * hgt * wid, d_out)

        def forward(self, x: Any) -> Any:  # x: (B, C, H, W)
            return self.out(self.net(x).flatten(1))

    class ResMLP(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            d = spec.d_hidden
            self.embs = nn.ModuleList(nn.Embedding(k, CAT_DIM) for k in cards)
            self.aux = None
            d_aux = 0
            if spec.aux == "sequence" and aux_shape is not None:
                self.aux = SequenceEncoder(aux_shape[-1], spec.d_aux)
                d_aux = spec.d_aux
            elif spec.aux == "surface" and aux_shape is not None:
                self.aux = SurfaceEncoder(aux_shape, spec.d_aux)
                d_aux = spec.d_aux
            self.stem = nn.Linear(2 * n_num + CAT_DIM * len(cards) + d_aux, d)
            self.blocks = nn.ModuleList(
                nn.Sequential(
                    nn.LayerNorm(d),
                    nn.Linear(d, 2 * d),
                    nn.ReLU(),
                    nn.Dropout(spec.dropout),
                    nn.Linear(2 * d, d),
                    nn.Dropout(spec.dropout),
                )
                for _ in range(spec.n_blocks)
            )
            self.norm = nn.LayerNorm(d)
            self.head = nn.Linear(d, len(HEADS))

        def forward(self, z: Any, miss: Any, cat: Any, aux: Any = None) -> Any:
            parts = [z, miss, *(e(cat[:, j]) for j, e in enumerate(self.embs))]
            if self.aux is not None:
                parts.append(self.aux(aux))
            h = self.stem(torch.cat(parts, dim=1))
            for b in self.blocks:
                h = h + b(h)
            return self.head(torch.relu(self.norm(h)))

    class FTTransformer(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            d = spec.d_token
            self.w = nn.Parameter(torch.randn(n_num, d) / d**0.5)
            self.m = nn.Parameter(torch.randn(n_num, d) / d**0.5)
            self.b = nn.Parameter(torch.zeros(n_num, d))
            self.embs = nn.ModuleList(nn.Embedding(k, d) for k in cards)
            self.cls = nn.Parameter(torch.randn(1, 1, d) / d**0.5)
            layer = nn.TransformerEncoderLayer(
                d,
                spec.ft_heads,
                dim_feedforward=2 * d,
                dropout=spec.dropout,
                batch_first=True,
                norm_first=True,
            )
            self.enc = nn.TransformerEncoder(layer, spec.ft_layers, enable_nested_tensor=False)
            self.norm = nn.LayerNorm(d)
            self.head = nn.Linear(d, len(HEADS))

        def forward(self, z: Any, miss: Any, cat: Any, aux: Any = None) -> Any:
            tok = z.unsqueeze(-1) * self.w + miss.unsqueeze(-1) * self.m + self.b
            cats = [e(cat[:, j]).unsqueeze(1) for j, e in enumerate(self.embs)]
            x = torch.cat([self.cls.expand(z.shape[0], -1, -1), tok, *cats], dim=1)
            h = self.enc(x)[:, 0]
            return self.head(torch.relu(self.norm(h)))

    return FTTransformer() if spec.trunk == "ft" else ResMLP()
