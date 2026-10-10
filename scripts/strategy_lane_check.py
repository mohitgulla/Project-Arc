"""Strategy-lane CI check (PLAN D44 "two lanes"; card E10.7).

    .venv/bin/python scripts/strategy_lane_check.py --base <sha> --head <sha> --body-file <f>

A pull request that touches a *strategy path* (``config/strategy_lane.yaml``) must say
which lane it is in, with one line in its body:

``Experiment: XP-<n>``
    The change is what experiment XP-<n> tests. The id must have a spec in
    ``config/experiments/live/``.
``Flag: <stem>.<path>``
    The change ships behind a NEW key in ``config/<stem>.yaml`` whose default is off
    (control behaviour). The check confirms the key is new and its value is off.
``Lane: fast — <reason>``
    A bug, safety or infra fix. arc-sentinel audits these.

A *promotion* (a PR that changes or removes a value that already exists in one of the
strategy YAMLs, i.e. flips a default) passes only with ``Experiment: XP-<n>`` whose
committed verdict file (``config/experiments/live/verdicts/XP-<n>.yaml``) says ``win``,
and every changed value must equal that experiment's treatment overlay. ``Flag:`` and
``Lane: fast`` never cover a promotion.

Deterministic: no network and no LLM. The only I/O is ``git`` on the local checkout
and reading files; :func:`evaluate` itself is pure, which is what the tests drive.
Exit 0 = pass (or not a strategy-lane PR), 1 = fail, 2 = usage / config error.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
LANE_CONFIG = REPO / "config" / "strategy_lane.yaml"

_EXPERIMENT_RE = re.compile(r"^\s*(?:[-*>]\s*)?experiment\s*:\s*(XP-[1-9]\d*)\b", re.I | re.M)
_FLAG_RE = re.compile(r"^\s*(?:[-*>]\s*)?flag\s*:\s*`?([A-Za-z0-9_.\-]+)`?", re.I | re.M)
_FAST_RE = re.compile(r"^\s*(?:[-*>]\s*)?lane\s*:\s*fast\b(.*)$", re.I | re.M)
_FAST_SEP = " \t—–-:`*"
_CONFIG_YAML_RE = re.compile(r"^config/([A-Za-z0-9_\-]+)\.yaml$")

# A committed verdict (config/experiments/live/verdicts/XP-<n>.yaml) is copied from the
# stored E10.3 report (`arc experiment show XP-<n> --json`); report_hash lets arc-sentinel
# match it to the `experiment_reports` row, which CI cannot read.
VERDICT_KEYS = frozenset({"experiment_id", "verdict", "report_hash"})

Leaf = tuple[str, ...]  # (stem, key, key, ...): one leaf value in config/<stem>.yaml


@dataclass(frozen=True)
class LaneConfig:
    strategy_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]
    experiments_dir: str
    verdicts_dir: str
    flag_off_values: tuple[Any, ...]
    fast_reason_min_chars: int
    promotion_stems: tuple[str, ...]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LaneConfig:
        known = {
            "strategy_paths",
            "exclude_paths",
            "experiments_dir",
            "verdicts_dir",
            "flag_off_values",
            "fast_reason_min_chars",
            "promotion_stems",
        }
        extra = set(data) - known
        if extra:
            msg = f"strategy_lane.yaml: unknown keys {sorted(extra)}"
            raise ValueError(msg)
        paths = tuple(str(p) for p in data.get("strategy_paths") or ())
        if not paths:
            msg = "strategy_lane.yaml: strategy_paths is empty"
            raise ValueError(msg)
        return cls(
            strategy_paths=paths,
            exclude_paths=tuple(str(p) for p in data.get("exclude_paths") or ()),
            experiments_dir=str(data.get("experiments_dir", "config/experiments/live")),
            verdicts_dir=str(data.get("verdicts_dir", "config/experiments/live/verdicts")),
            flag_off_values=tuple(data.get("flag_off_values", (False, None))),
            fast_reason_min_chars=int(data.get("fast_reason_min_chars", 10)),
            promotion_stems=tuple(str(s) for s in data.get("promotion_stems") or ()),
        )


@dataclass(frozen=True)
class Experiment:
    """What the check needs from a spec and its committed verdict (if any)."""

    id: str
    overlay: Mapping[str, Any]  # arms.treatment.overlay: stem -> partial file
    verdict: str | None = None


@dataclass(frozen=True)
class Lanes:
    experiments: tuple[str, ...]
    flags: tuple[str, ...]
    fast_reason: str | None


@dataclass
class Result:
    strategy_files: list[str]
    ok: bool
    lane: str  # none | experiment | flag | fast | promotion
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = "PASS" if self.ok else "FAIL"
        if not self.strategy_files:
            return "strategy-lane: PASS (no strategy paths touched)"
        lines = [f"strategy-lane: {head} (lane: {self.lane})", "strategy paths touched:"]
        lines += [f"  {p}" for p in self.strategy_files]
        lines += [f"note: {n}" for n in self.notes]
        lines += [f"error: {e}" for e in self.errors]
        if not self.ok:
            lines.append(
                "fix: add one of `Experiment: XP-<n>`, `Flag: <stem>.<key>` or "
                "`Lane: fast — <reason>` to the PR body (docs/OPS.md 5.20), then re-run "
                "the strategy-lane job"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


def strategy_files(changed: Iterable[str], cfg: LaneConfig) -> list[str]:
    """The changed paths that are strategy lane (sorted, unique)."""
    out = {
        p
        for p in changed
        if any(fnmatch.fnmatchcase(p, g) for g in cfg.strategy_paths)
        and not any(fnmatch.fnmatchcase(p, g) for g in cfg.exclude_paths)
    }
    return sorted(out)


def parse_body(body: str) -> Lanes:
    body = body.replace("**", "").replace("__", "")  # markdown bold around the label
    fast = _FAST_RE.search(body)
    return Lanes(
        experiments=tuple(dict.fromkeys(m.upper() for m in _EXPERIMENT_RE.findall(body))),
        flags=tuple(dict.fromkeys(_FLAG_RE.findall(body))),
        fast_reason=fast.group(1).strip().strip(_FAST_SEP).strip() if fast else None,
    )


def flatten(stem: str, data: Any) -> dict[Leaf, Any]:
    """Leaf paths of one YAML document; lists and scalars are leaves."""
    out: dict[Leaf, Any] = {}

    def walk(prefix: Leaf, node: Any) -> None:
        if isinstance(node, dict) and node:
            for k, v in node.items():
                walk((*prefix, str(k)), v)
        else:
            out[prefix] = node

    walk((stem,), data if data is not None else {})
    if out == {(stem,): {}}:
        return {}
    return out


@dataclass(frozen=True)
class YamlDelta:
    added: dict[Leaf, Any]
    changed: dict[Leaf, tuple[Any, Any]]  # leaf -> (old, new)
    removed: dict[Leaf, Any]


def yaml_delta(stem: str, old: Any, new: Any) -> YamlDelta:
    a, b = flatten(stem, old), flatten(stem, new)
    return YamlDelta(
        added={k: v for k, v in b.items() if k not in a},
        changed={k: (a[k], b[k]) for k in a.keys() & b.keys() if a[k] != b[k]},
        removed={k: v for k, v in a.items() if k not in b},
    )


def _dotted(leaf: Leaf) -> str:
    return ".".join(leaf)


def _overlay_value(overlay: Mapping[str, Any], leaf: Leaf) -> tuple[bool, Any]:
    node: Any = overlay
    for k in leaf:
        if not isinstance(node, Mapping) or k not in node:
            return False, None
        node = node[k]
    return True, node


def _is_off(value: Any, cfg: LaneConfig) -> bool:
    for off in cfg.flag_off_values:
        if isinstance(off, str) and isinstance(value, str):
            if value.strip().lower() == off.lower():
                return True
        elif type(value) is type(off) and value == off:
            return True
    return False


def evaluate(
    changed: Iterable[str],
    body: str,
    deltas: Mapping[str, YamlDelta],
    experiments: Mapping[str, Experiment],
    cfg: LaneConfig,
) -> Result:
    """Decide one PR.

    *changed* are the repo-relative paths the PR touches, *deltas* the leaf-level
    YAML changes per strategy ``config/<stem>.yaml`` (keyed by stem), *experiments*
    every spec in the experiments dir (keyed by id, with its verdict if committed).
    """
    files = strategy_files(changed, cfg)
    if not files:
        return Result(strategy_files=[], ok=True, lane="none")

    lanes = parse_body(body)
    errors: list[str] = []
    notes: list[str] = []

    unknown = [x for x in lanes.experiments if x not in experiments]
    errors += [f"Experiment: {x} has no spec in {cfg.experiments_dir}/" for x in unknown]
    cited = [experiments[x] for x in lanes.experiments if x in experiments]

    fast_ok = False
    if lanes.fast_reason is not None:
        if len(lanes.fast_reason) >= cfg.fast_reason_min_chars:
            fast_ok = True
        else:
            errors.append(
                f"`Lane: fast` needs a reason of at least {cfg.fast_reason_min_chars} characters"
            )

    flag_ok: list[str] = []
    all_added = {_dotted(k): v for d in deltas.values() for k, v in d.added.items()}
    all_old = {_dotted(k) for d in deltas.values() for k in (*d.changed.keys(), *d.removed.keys())}
    for flag in lanes.flags:
        stem = flag.split(".", 1)[0]
        if flag in all_old:
            errors.append(f"Flag: {flag} already exists on the base branch; a flag must be new")
        elif flag not in all_added:
            errors.append(
                f"Flag: {flag} is not a key this PR adds to config/{stem}.yaml "
                "(the key must be new and set to its control default)"
            )
        elif not _is_off(all_added[flag], cfg):
            errors.append(
                f"Flag: {flag} defaults to {all_added[flag]!r}; a new flag must default to off "
                f"(one of {list(cfg.flag_off_values)}) so control behaviour is unchanged"
            )
        else:
            flag_ok.append(flag)

    # -- promotion: an existing value in a strategy YAML changed or went away -----
    promo = {s: d for s, d in deltas.items() if s in cfg.promotion_stems}
    changed_leaves = {k: v for d in promo.values() for k, v in d.changed.items()}
    removed_leaves = {k: v for d in promo.values() for k, v in d.removed.items()}
    if changed_leaves or removed_leaves:
        winners = [e for e in cited if e.verdict == "win"]
        for e in cited:
            if e.verdict != "win":
                notes.append(f"{e.id} verdict is {e.verdict or 'not committed'}, not win")
        if not winners:
            listed = ", ".join(_dotted(k) for k in sorted({*changed_leaves, *removed_leaves}))
            errors.append(
                f"promotion: this PR changes existing strategy values ({listed}); it needs "
                "`Experiment: XP-<n>` with a committed `win` verdict "
                f"({cfg.verdicts_dir}/XP-<n>.yaml). Flag: and Lane: fast do not cover a promotion"
            )
            return Result(files, ok=False, lane="promotion", errors=errors, notes=notes)
        for leaf, (old, new) in sorted(changed_leaves.items()):
            matched = False
            for e in winners:
                present, val = _overlay_value(e.overlay, leaf)
                if present and val == new:
                    matched = True
                    break
            if not matched:
                ids = ", ".join(e.id for e in winners)
                errors.append(
                    f"promotion: {_dotted(leaf)} {old!r} -> {new!r} is not the treatment "
                    f"overlay value of {ids}; only what the winning experiment tested may change"
                )
        for leaf in sorted(removed_leaves):
            notes.append(f"removed {_dotted(leaf)} under winning experiment(s)")
        notes.append("promotes " + ", ".join(e.id for e in winners))
        return Result(files, ok=not errors, lane="promotion", errors=errors, notes=notes)

    # -- non-promotion: any one valid lane passes -------------------------------
    if cited and not unknown:
        lane = "experiment"
    elif flag_ok and len(flag_ok) == len(lanes.flags):
        lane = "flag"
        notes.append("arc-sentinel checks the code path is reachable only when the flag is on")
    elif fast_ok:
        lane = "fast"
        notes.append(f"fast lane: {lanes.fast_reason} (arc-sentinel audits fast-lane PRs)")
    else:
        if not (lanes.experiments or lanes.flags or lanes.fast_reason is not None):
            errors.append("no lane line in the PR body")
        return Result(files, ok=False, lane="none", errors=errors, notes=notes)
    # A lane passed; a broken extra line is still an error (it would mislead the audit).
    return Result(files, ok=not errors, lane=lane, errors=errors, notes=notes)


# ---------------------------------------------------------------------------
# I/O: config, specs, verdicts and git
# ---------------------------------------------------------------------------


def load_lane_config(path: Path = LANE_CONFIG) -> LaneConfig:
    return LaneConfig.from_mapping(yaml.safe_load(path.read_text()) or {})


def load_experiments(root: Path, cfg: LaneConfig) -> dict[str, Experiment]:
    """Every spec in the experiments dir, with its committed verdict when present."""
    verdicts: dict[str, str] = {}
    vdir = root / cfg.verdicts_dir
    if vdir.is_dir():
        for p in sorted(vdir.glob("*.yaml")):
            data = yaml.safe_load(p.read_text()) or {}
            if not isinstance(data, dict) or not data.keys() >= VERDICT_KEYS:
                msg = f"{p}: a verdict file needs {', '.join(sorted(VERDICT_KEYS))}"
                raise ValueError(msg)
            verdicts[str(data["experiment_id"]).upper()] = str(data["verdict"]).lower()
    out: dict[str, Experiment] = {}
    edir = root / cfg.experiments_dir
    for p in sorted(edir.glob("*.yaml")) if edir.is_dir() else ():
        data = yaml.safe_load(p.read_text()) or {}
        if not isinstance(data, dict) or "id" not in data:
            continue
        xid = str(data["id"]).upper()
        arms = data.get("arms") or {}
        arms = arms if isinstance(arms, dict) else {}
        treatment = arms.get("treatment")
        if treatment is None:  # D69 spec v2: `treatments: {t1: ...}`
            ts = arms.get("treatments") or {}
            # a K>1 spec has no single tested overlay until its verdict names the winning
            # arm (E15.5): nothing is promotable from it yet
            treatment = next(iter(ts.values())) if isinstance(ts, dict) and len(ts) == 1 else {}
        overlay = treatment.get("overlay") or {} if isinstance(treatment, dict) else {}
        out[xid] = Experiment(id=xid, overlay=overlay, verdict=verdicts.get(xid))
    return out


GitRunner = Callable[[list[str]], str]


def _git(root: Path) -> GitRunner:
    def run(args: list[str]) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout

    return run


def _show(git: GitRunner, rev: str, path: str) -> Any:
    try:
        text = git(["show", f"{rev}:{path}"])
    except subprocess.CalledProcessError:
        return None  # absent at that rev (added or deleted file)
    return yaml.safe_load(text)


def collect(
    git: GitRunner, base: str, head: str, cfg: LaneConfig
) -> tuple[list[str], dict[str, YamlDelta]]:
    """Changed paths between the merge base and *head*, plus leaf deltas per changed
    top-level ``config/<stem>.yaml`` (strategy files for promotions, any file for a
    ``Flag:`` key such as one in ``routines.yaml``)."""
    mb = git(["merge-base", base, head]).strip()
    changed = [p for p in git(["diff", "--name-only", mb, head]).splitlines() if p]
    deltas: dict[str, YamlDelta] = {}
    for p in changed:
        m = _CONFIG_YAML_RE.match(p)
        if m is None or m.group(1) == "strategy_lane":
            continue
        stem = m.group(1)
        deltas[stem] = yaml_delta(stem, _show(git, mb, p), _show(git, head, p))
    return changed, deltas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Strategy-lane CI check (PLAN D44, card E10.7).")
    ap.add_argument("--base", required=True, help="base commit (PR base sha)")
    ap.add_argument("--head", required=True, help="head commit (PR head sha)")
    ap.add_argument("--body-file", type=Path, required=True, help="PR body text")
    ap.add_argument("--repo", type=Path, default=REPO)
    args = ap.parse_args(argv)
    try:
        cfg = load_lane_config(args.repo / "config" / "strategy_lane.yaml")
        experiments = load_experiments(args.repo, cfg)
        changed, deltas = collect(_git(args.repo), args.base, args.head, cfg)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"strategy-lane: config error: {exc}\n")
        return 2
    result = evaluate(changed, args.body_file.read_text(), deltas, experiments, cfg)
    sys.stdout.write(result.render() + "\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
