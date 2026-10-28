"""Training loop of the E27.6 challengers: weighted multi-task loss, early stopping on
the inner validation block, fixed-epoch refit, batched prediction.

Determinism: :func:`setup` seeds torch / numpy, fixes the CPU thread count and turns on
``torch.use_deterministic_algorithms``; batches come from a seeded numpy permutation.
On CPU the same inputs give byte-identical weights and predictions (tested). MPS kernels
are not bit-deterministic; challengers trained there report seed variance instead
(``docs/RESEARCH/ml-dl-challengers.md``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from arc.ml.dl.nets import HEADS, build_net, load_torch

if TYPE_CHECKING:
    from collections.abc import Callable

    from arc.ml.dl.config import LossWeights, NetSpec

#: target array columns
Y_COLS: tuple[str, ...] = ("win", "stop", "r", "days")
DAYS_SCALE = 30.0
QUANTS = {"q10": 0.1, "q50": 0.5, "q90": 0.9}


@dataclass
class Batchable:
    """Row-aligned model inputs; *aux* is a callable ``idx -> (B, ...) float32`` (the
    sequence / surface gather) or None."""

    z: np.ndarray
    miss: np.ndarray
    cat: np.ndarray
    y: np.ndarray  # (n, 4): win, stop, r, days (NaN = missing)
    w: np.ndarray
    aux: Callable[[np.ndarray], np.ndarray] | None = None
    aux_shape: tuple[int, ...] | None = None

    def __len__(self) -> int:
        return len(self.z)

    def take(self, idx: np.ndarray) -> Batchable:
        aux: Callable[[np.ndarray], np.ndarray] | None = None
        if self.aux is not None:
            base = self.aux

            def _aux(i: np.ndarray) -> np.ndarray:
                return base(idx[i])

            aux = _aux

        return Batchable(
            self.z[idx],
            self.miss[idx],
            self.cat[idx],
            self.y[idx],
            self.w[idx],
            aux,
            self.aux_shape,
        )


def resolve_device(spec: NetSpec) -> str:
    torch = load_torch()
    if spec.device == "auto":
        return "mps" if torch.backends.mps.is_available() else "cpu"
    if spec.device == "mps" and not torch.backends.mps.is_available():
        return "cpu"
    return spec.device


def setup(seed: int, threads: int) -> None:
    torch = load_torch()
    torch.manual_seed(seed)
    torch.set_num_threads(threads)
    torch.use_deterministic_algorithms(True, warn_only=True)


def _loss(out: Any, y: Any, w: Any, lw: LossWeights, huber: float) -> Any:
    torch = load_torch()
    f = torch.nn.functional
    total = out.new_zeros(())
    j = {h: i for i, h in enumerate(HEADS)}

    def wmean(v: Any, m: Any) -> Any:
        ww = w * m
        return (v * ww).sum() / ww.sum().clamp_min(1e-9)

    def ok(col: Any) -> Any:
        return torch.isfinite(col).float()

    yw, ys, yr, yd = y[:, 0], y[:, 1], y[:, 2], y[:, 3]
    mw, ms, mr, md = ok(yw), ok(ys), ok(yr), ok(yd)
    yw0, ys0, yr0, yd0 = (torch.nan_to_num(t) for t in (yw, ys, yr, yd))
    bce = f.binary_cross_entropy_with_logits
    total = total + lw.win * wmean(bce(out[:, j["win"]], yw0, reduction="none"), mw)
    total = total + lw.stop * wmean(bce(out[:, j["stop"]], ys0, reduction="none"), ms)
    hub = f.huber_loss
    total = total + lw.r * wmean(hub(out[:, j["r"]], yr0, reduction="none", delta=huber), mr)
    for q, a in QUANTS.items():
        e = yr0 - out[:, j[q]]
        total = total + lw.quantiles * wmean(torch.maximum(a * e, (a - 1.0) * e), mr) / 3.0
    total = total + lw.days * wmean((out[:, j["days"]] - yd0 / DAYS_SCALE) ** 2, md)
    pos = mr * (yr0 > 0).float()
    neg = mr * (yr0 <= 0).float()
    for h, m in (("r_win", pos), ("r_loss", neg)):
        total = total + lw.conditional * wmean(
            hub(out[:, j[h]], yr0, reduction="none", delta=huber), m
        )
    return total


def _tensors(d: Batchable, idx: np.ndarray, dev: str) -> tuple[Any, ...]:
    torch = load_torch()
    aux = None
    if d.aux is not None:
        aux = torch.from_numpy(np.ascontiguousarray(d.aux(idx), dtype=np.float32)).to(dev)
    return (
        torch.from_numpy(d.z[idx]).to(dev),
        torch.from_numpy(d.miss[idx]).to(dev),
        torch.from_numpy(d.cat[idx]).to(dev),
        aux,
        torch.from_numpy(d.y[idx].astype(np.float32)).to(dev),
        torch.from_numpy(d.w[idx].astype(np.float32)).to(dev),
    )


def eval_batch(spec: NetSpec) -> int:
    """Inference batch: the FT's attention is O(batch × tokens²), so it stays at its
    training batch; the conv / MLP trunks use 8192."""
    return spec.batch if spec.trunk == "ft" else 8192


def evaluate(net: Any, d: Batchable, spec: NetSpec, lw: LossWeights, huber: float, dev: str):
    torch = load_torch()
    net.eval()
    tot, n = 0.0, 0.0
    bs = eval_batch(spec)
    with torch.no_grad():
        for i in range(0, len(d), bs):
            idx = np.arange(i, min(i + bs, len(d)))
            z, m, c, a, y, w = _tensors(d, idx, dev)
            out = net(z, m, c, a)
            ws = float(w.sum())
            tot += float(_loss(out, y, w, lw, huber)) * ws
            n += ws
    return tot / max(n, 1e-9)


@dataclass
class FitResult:
    state: dict[str, Any]
    epochs: int
    val_curve: list[float] = field(default_factory=list)
    seconds: float = 0.0
    device: str = "cpu"


def fit(
    spec: NetSpec,
    lw: LossWeights,
    train: Batchable,
    *,
    seed: int,
    huber: float,
    threads: int,
    cards: list[int],
    val: Batchable | None = None,
    epochs: int | None = None,
    say: Callable[[str], None] = lambda _m: None,
) -> FitResult:
    """Early-stopped on *val* (best epoch kept) or exactly *epochs* epochs."""
    torch = load_torch()
    setup(seed, threads)
    dev = resolve_device(spec)
    net = build_net(spec, n_num=train.z.shape[1], cards=cards, aux_shape=train.aux_shape).to(dev)
    opt = torch.optim.AdamW(net.parameters(), lr=spec.lr, weight_decay=spec.weight_decay)
    rng = np.random.default_rng(seed)
    n_ep = epochs if epochs is not None else spec.max_epochs
    best, best_ep, best_state, bad = np.inf, 0, None, 0
    curve: list[float] = []
    t0 = time.monotonic()
    for ep in range(1, n_ep + 1):
        net.train()
        perm = rng.permutation(len(train))
        for i in range(0, len(perm), spec.batch):
            idx = np.sort(perm[i : i + spec.batch])
            z, m, c, a, y, w = _tensors(train, idx, dev)
            opt.zero_grad(set_to_none=True)
            loss = _loss(net(z, m, c, a), y, w, lw, huber)
            loss.backward()
            opt.step()
        if val is None:
            continue
        vl = evaluate(net, val, spec, lw, huber, dev)
        curve.append(vl)
        say(f"      ep {ep} val {vl:.5f} ({time.monotonic() - t0:.0f}s)")
        if vl < best - 1e-5:
            best, best_ep, bad = vl, ep, 0
            best_state = {k: v.detach().cpu().clone() for k, v in net.state_dict().items()}
        else:
            bad += 1
            if bad >= spec.patience:
                break
    if val is None or best_state is None:
        best_state = {k: v.detach().cpu().clone() for k, v in net.state_dict().items()}
        best_ep = n_ep
    return FitResult(best_state, best_ep, curve, time.monotonic() - t0, dev)


def predict(
    spec: NetSpec, state: dict[str, Any], d: Batchable, *, cards: list[int], device: str
) -> np.ndarray:
    """``(n, len(HEADS))`` raw outputs (win/stop as probabilities, days in sessions)."""
    torch = load_torch()
    net = build_net(spec, n_num=d.z.shape[1], cards=cards, aux_shape=d.aux_shape)
    net.load_state_dict(state)
    net = net.to(device).eval()
    outs = []
    bs = eval_batch(spec)
    with torch.no_grad():
        for i in range(0, len(d), bs):
            idx = np.arange(i, min(i + bs, len(d)))
            z, m, c, a, _y, _w = _tensors(d, idx, device)
            outs.append(net(z, m, c, a).float().cpu().numpy())
    o = np.concatenate(outs) if outs else np.zeros((0, len(HEADS)), dtype=np.float32)
    o = o.astype(np.float64)
    j = {h: i for i, h in enumerate(HEADS)}
    for h in ("win", "stop"):
        o[:, j[h]] = 1.0 / (1.0 + np.exp(-o[:, j[h]]))
    o[:, j["days"]] = o[:, j["days"]] * DAYS_SCALE
    return o
