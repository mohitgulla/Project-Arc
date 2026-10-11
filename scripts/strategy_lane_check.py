"""Strategy-lane CI check (PLAN D86, revising D44 / E10.7; card E21.1).

    .venv/bin/python scripts/strategy_lane_check.py --base <sha> --head <sha> --body-file <f>

Dev changes ship on, to every experiment arm (D86). The check has one hard rule and
is advisory for everything else:

**Hard rule: locked leaves.** An *open* experiment is a spec in
``config/experiments/live/`` with no committed verdict file (``verdicts/``) on the base
branch. Its *locked leaves* are the union of every treatment overlay's leaves (v1
``arms.treatment``, v2 ``arms.treatments.t<k>``). A PR fails when it adds, changes or
removes a locked leaf's value in the corresponding ``config/<stem>.yaml`` unless it is
that experiment's promotion: the body cites ``Experiment: XP-<n>``, the verdict file at
the PR head says ``verdict: win`` with ``winner: t<k>`` (a single-treatment verdict
without ``winner`` maps to ``t1``), and every changed value equals arm ``t<k>``'s
overlay value. Removing a locked value never passes.

**Advisory, never fails.** A PR touching a ``strategy_paths`` file gets a note (and, in
GitHub Actions, a job summary and a PR annotation) listing the strategy files and the
strategy-YAML leaves it changes, and asks for ``XP-advisory: none | <reason>`` in the
body; a missing line is a warning only. Old lane lines (``Flag:``, ``Lane: fast``,
``Experiment:``) are accepted and ignored, except ``Experiment:`` for a promotion.

Open-ness is decided on the merge base: a spec the PR itself adds locks nothing yet, and
an open spec the PR edits keeps its base leaves locked. A PR that deletes an open spec
(retiring a never-registered draft, E20.1 / E21.2) unlocks its leaves with a warning that
arc-sentinel audits. The verdict and winning arm are read at the PR head, so a
promotion PR commits its verdict file.

Deterministic: no network and no LLM. The only I/O is ``git`` on the local checkout
and reading files; :func:`evaluate` itself is pure, which is what the tests drive.
Exit 0 = pass (or not a strategy-lane PR), 1 = fail, 2 = usage / config error.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
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
_ADVISORY_RE = re.compile(r"^\s*(?:[-*>]\s*)?xp-advisory\s*:(.*)$", re.I | re.M)
_ADVISORY_SEP = " \t—–-:`*"
_CONFIG_YAML_RE = re.compile(r"^config/([A-Za-z0-9_\-]+)\.yaml$")
_WINNER_RE = re.compile(r"^t([1-9]|1[0-6])$")

# A committed verdict (config/experiments/live/verdicts/XP-<n>.yaml) is copied from the
# stored E10.3 report (`arc experiment show XP-<n> --json`); report_hash lets arc-sentinel
# match it to the `experiment_reports` row, which CI cannot read. ``winner: t<k>`` names
# the winning arm of a multi-treatment (D69 v2) experiment.
VERDICT_KEYS = frozenset({"experiment_id", "verdict", "report_hash"})
FIRST_TREATMENT = "t1"

Leaf = tuple[str, ...]  # (stem, key, key, ...): one leaf value in config/<stem>.yaml


@dataclass(frozen=True)
class LaneConfig:
    strategy_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]
    experiments_dir: str
    verdicts_dir: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LaneConfig:
        known = {"strategy_paths", "exclude_paths", "experiments_dir", "verdicts_dir"}
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
        )


@dataclass(frozen=True)
class Experiment:
    """What the check needs from a spec and its committed verdict (if any)."""

    id: str
    arms: Mapping[str, Mapping[str, Any]]  # treatment arm (t1..) -> overlay (stem -> partial)
    verdict: str | None = None  # at the PR head
    winner: str | None = None  # t<k>; None on a single-treatment verdict (= t1)
    open_at_base: bool = True  # no verdict on the base branch (the PR may add one)
    path: str = ""  # repo-relative spec path

    def winning_arm(self) -> str:
        return self.winner or FIRST_TREATMENT


@dataclass(frozen=True)
class Body:
    experiments: tuple[str, ...]
    advisory: str | None  # the text after `XP-advisory:`, "" when empty, None when absent


@dataclass
class Result:
    strategy_files: list[str]
    ok: bool
    status: str  # none | advisory | promotion | locked
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    leaves: list[str] = field(default_factory=list)  # strategy-YAML leaves changed
    advisory: str | None = None

    def render(self) -> str:
        head = "PASS" if self.ok else "FAIL"
        if not self.strategy_files and self.ok and not (self.notes or self.warnings):
            return "strategy-lane: PASS (no strategy paths touched)"
        lines = [f"strategy-lane: {head} ({self.status})"]
        if self.strategy_files:
            lines.append("strategy paths touched:")
            lines += [f"  {p}" for p in self.strategy_files]
        if self.leaves:
            lines.append("strategy config leaves changed:")
            lines += [f"  {x}" for x in self.leaves]
        if self.advisory is not None:
            lines.append(f"XP-advisory: {self.advisory or '(empty)'}")
        lines += [f"note: {n}" for n in self.notes]
        lines += [f"warning: {w}" for w in self.warnings]
        lines += [f"error: {e}" for e in self.errors]
        if not self.ok:
            lines.append(
                "fix: leave the locked values alone until the experiment has a verdict, or "
                "make this PR the promotion (`Experiment: XP-<n>` + a `win` verdict file + "
                "the winning arm's values); docs/OPS.md 5.20"
            )
        return "\n".join(lines)

    def summary_markdown(self) -> str:
        """The GitHub job summary (``$GITHUB_STEP_SUMMARY``)."""
        lines = [f"### strategy-lane: {'PASS' if self.ok else 'FAIL'} ({self.status})", ""]
        if self.strategy_files:
            lines.append("Strategy files changed (this ships to every experiment arm, D86):")
            lines += [f"- `{p}`" for p in self.strategy_files]
        if self.leaves:
            lines += ["", "Strategy config leaves changed:"]
            lines += [f"- `{x}`" for x in self.leaves]
        if self.strategy_files:
            lines += [
                "",
                f"XP-advisory: {self.advisory}"
                if self.advisory
                else "**No `XP-advisory:` line.** Add `XP-advisory: none` or "
                "`XP-advisory: <why this might deserve an experiment>` to the PR body.",
            ]
        lines += ["", *(f"- note: {n}" for n in self.notes)] if self.notes else []
        lines += [f"- warning: {w}" for w in self.warnings]
        lines += [f"- **error:** {e}" for e in self.errors]
        return "\n".join(lines) + "\n"

    def annotations(self) -> list[str]:
        """GitHub workflow commands: one notice/warning per PR, an error per failure."""
        out: list[str] = []
        if self.strategy_files:
            what = ", ".join(self.strategy_files[:6]) + (
                f" (+{len(self.strategy_files) - 6} more)" if len(self.strategy_files) > 6 else ""
            )
            if self.advisory:
                out.append(
                    f"::notice title=strategy-lane advisory::Strategy change ships to all "
                    f"arms: {what}. XP-advisory: {self.advisory}"
                )
            else:
                out.append(
                    f"::warning title=strategy-lane advisory::Strategy change ships to all "
                    f"arms: {what}. Add `XP-advisory: none | <reason>` to the PR body."
                )
        out += [f"::warning title=strategy-lane::{w}" for w in self.warnings]
        out += [f"::error title=strategy-lane::{e}" for e in self.errors]
        return out


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


def parse_body(body: str) -> Body:
    body = body.replace("**", "").replace("__", "")  # markdown bold around the label
    adv = _ADVISORY_RE.search(body)
    return Body(
        experiments=tuple(dict.fromkeys(m.upper() for m in _EXPERIMENT_RE.findall(body))),
        advisory=adv.group(1).strip().strip(_ADVISORY_SEP).strip() if adv else None,
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

    def leaves(self) -> set[Leaf]:
        return {*self.added, *self.changed, *self.removed}


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


def locked_leaves(exp: Experiment) -> set[Leaf]:
    """The union of every treatment overlay's leaves (stem first)."""
    out: set[Leaf] = set()
    for overlay in exp.arms.values():
        for stem, partial in (overlay or {}).items():
            out |= {k for k, v in flatten(str(stem), partial).items() if v != {}}
    return out


def _overlaps(a: Leaf, b: Leaf) -> bool:
    """Same leaf, or one is inside the other (a list/scalar replaced by a mapping)."""
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def evaluate(
    changed: Iterable[str],
    body: str,
    deltas: Mapping[str, YamlDelta],
    experiments: Mapping[str, Experiment],
    cfg: LaneConfig,
    removed_specs: Iterable[str] = (),
) -> Result:
    """Decide one PR.

    *changed* are the repo-relative paths the PR touches, *deltas* the leaf-level YAML
    changes per changed ``config/<stem>.yaml`` (keyed by stem), *experiments* every
    known spec (keyed by id; ``open_at_base`` and the head verdict, see
    :func:`build_experiments`), *removed_specs* the open specs the PR deletes.
    """
    changed = list(changed)
    files = strategy_files(changed, cfg)
    parsed = parse_body(body)
    errors: list[str] = []
    notes: list[str] = []
    warnings: list[str] = []

    # -- hard rule: leaves an open experiment tests ---------------------------------
    delta_leaves: dict[Leaf, tuple[str, Any]] = {}  # leaf -> (kind, new value)
    for d in deltas.values():
        delta_leaves |= {k: ("added", v) for k, v in d.added.items()}
        delta_leaves |= {k: ("changed", new) for k, (_old, new) in d.changed.items()}
        delta_leaves |= {k: ("removed", None) for k in d.removed}
    promoted: list[str] = []
    touched_locked = False
    for xid in sorted(experiments, key=lambda x: int(x.split("-")[1])):
        exp = experiments[xid]
        if not exp.open_at_base:
            continue
        locks = locked_leaves(exp)
        hits = sorted(leaf for leaf in delta_leaves if any(_overlaps(leaf, k) for k in locks))
        if not hits:
            continue
        touched_locked = True
        listed = ", ".join(_dotted(x) for x in hits)
        if xid not in parsed.experiments:
            errors.append(
                f"{xid} is open (no verdict) and tests {listed}; this PR changes it. "
                f"Leave it until {xid} concludes, or cite `Experiment: {xid}` with its "
                "`win` verdict to promote the winning arm"
            )
            continue
        if exp.verdict != "win":
            errors.append(
                f"Experiment: {xid} cited, but its verdict is "
                f"{exp.verdict or 'not committed'}, not win; {listed} stay locked"
            )
            continue
        arm = exp.winning_arm()
        overlay = exp.arms.get(arm)
        if overlay is None:
            errors.append(f"{xid} verdict names winner {arm}, which is not an arm of its spec")
            continue
        bad = False
        for leaf in hits:
            kind, new = delta_leaves[leaf]
            if kind == "removed":
                errors.append(f"promotion {xid}: removing {_dotted(leaf)} is never a promotion")
                bad = True
                continue
            present, val = _overlay_value(overlay, leaf)
            if not present or val != new:
                tested = repr(val) if present else "untested by that arm"
                errors.append(
                    f"promotion {xid}: {_dotted(leaf)} -> {new!r} is not arm {arm}'s "
                    f"overlay value ({tested}); only what the winning arm tested may ship"
                )
                bad = True
        if not bad:
            promoted.append(f"{xid} ({arm})")
    if promoted:
        notes.append("promotes " + ", ".join(promoted))
    for path in sorted(removed_specs):
        warnings.append(
            f"deletes open experiment spec {path} (no verdict), so its leaves unlock; "
            "arc-sentinel checks it was never registered"
        )

    # -- advisory -------------------------------------------------------------------
    stems = {m.group(1) for p in files if (m := _CONFIG_YAML_RE.match(p))}
    leaves = sorted(_dotted(k) for s, d in deltas.items() if s in stems for k in d.leaves())
    unknown = [x for x in parsed.experiments if x not in experiments]
    notes += [f"Experiment: {x} has no spec in {cfg.experiments_dir}/ (ignored)" for x in unknown]
    if files and not parsed.advisory:
        warnings.append(
            "no `XP-advisory: none | <reason>` line in the PR body (advisory only; "
            "docs/OPS.md 5.20)"
        )
    if errors:
        status = "locked"
    elif promoted:
        status = "promotion"
    elif files or touched_locked or warnings:
        status = "advisory"
    else:
        status = "none"
    return Result(
        strategy_files=files,
        ok=not errors,
        status=status,
        errors=errors,
        notes=notes,
        warnings=warnings,
        leaves=leaves,
        advisory=parsed.advisory if files else None,
    )


# ---------------------------------------------------------------------------
# I/O: config, specs, verdicts and git
# ---------------------------------------------------------------------------


def load_lane_config(path: Path = LANE_CONFIG) -> LaneConfig:
    return LaneConfig.from_mapping(yaml.safe_load(path.read_text()) or {})


def _merge(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Deep merge, *b* wins on a conflicting scalar (same rule as arc.utils.yamlpatch)."""
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


def _arms(data: Any) -> dict[str, Mapping[str, Any]]:
    """Treatment arm -> overlay. A v1 ``arms.treatment`` is ``t1`` (the D69 alias)."""
    arms = data.get("arms") if isinstance(data, dict) else None
    arms = arms if isinstance(arms, dict) else {}
    out: dict[str, Mapping[str, Any]] = {}
    if isinstance(arms.get("treatment"), dict):  # v1 spec: the one treatment is t1
        out[FIRST_TREATMENT] = arms["treatment"].get("overlay") or {}
    ts = arms.get("treatments")
    if isinstance(ts, dict):  # D69 spec v2
        for name, arm in ts.items():
            if isinstance(arm, dict):
                out[str(name)] = arm.get("overlay") or {}
    return out


@dataclass(frozen=True)
class Verdict:
    verdict: str
    winner: str | None


def parse_verdict(data: Any, where: str) -> tuple[str, Verdict]:
    """``(experiment id, verdict)`` from a committed verdict file's YAML."""
    if not isinstance(data, dict) or not data.keys() >= VERDICT_KEYS:
        msg = f"{where}: a verdict file needs {', '.join(sorted(VERDICT_KEYS))}"
        raise ValueError(msg)
    winner = data.get("winner")
    if winner is not None and not _WINNER_RE.match(str(winner)):
        msg = f"{where}: winner must be t1..t16, not {winner!r}"
        raise ValueError(msg)
    return str(data["experiment_id"]).upper(), Verdict(
        verdict=str(data["verdict"]).lower(), winner=None if winner is None else str(winner)
    )


def build_experiments(
    base_specs: Mapping[str, Any],
    head_specs: Mapping[str, Any],
    base_verdicts: Mapping[str, Verdict],
    head_verdicts: Mapping[str, Verdict],
) -> tuple[dict[str, Experiment], list[str]]:
    """The experiments the check knows, and the open specs this PR deletes.

    Specs are keyed by repo path (parsed YAML). An experiment is *open* when its spec is
    on the base, still on the head, and the base has no verdict for it. Its locked
    leaves come from the base and head specs together, so editing an open spec in the
    same PR unlocks nothing. A spec the PR adds locks nothing yet. A PR that deletes an
    open spec (retiring a never-registered draft, e.g. E20.1 / E21.2) unlocks its
    leaves: it is listed as a warning and arc-sentinel checks the id was never
    registered. The verdict (and winning arm) come from the PR head, so a promotion PR
    commits its verdict file.
    """
    out: dict[str, Experiment] = {}
    removed: list[str] = []
    for path in sorted({*base_specs, *head_specs}):
        base, head = base_specs.get(path), head_specs.get(path)
        base = base if isinstance(base, dict) and "id" in base else None
        head = head if isinstance(head, dict) and "id" in head else None
        spec = head or base
        if spec is None:
            continue
        xid = str(spec["id"]).upper()
        on_base = base is not None and str(base["id"]).upper() == xid
        if on_base and head is None and xid not in base_verdicts:
            removed.append(f"{path} ({xid})")
            continue
        is_open = on_base and xid not in base_verdicts
        arms: dict[str, Mapping[str, Any]] = {}
        for src in (base if is_open else None, head):
            for arm, overlay in _arms(src).items():
                arms[arm] = _merge(arms.get(arm, {}), overlay)
        v = head_verdicts.get(xid)
        out[xid] = Experiment(
            id=xid,
            arms=arms,
            verdict=v.verdict if v else None,
            winner=v.winner if v else None,
            open_at_base=is_open,
            path=path,
        )
    return out, removed


def load_experiments(root: Path, cfg: LaneConfig) -> dict[str, Experiment]:
    """Every spec in the working tree's experiments dir with its verdict (no git).

    Base = head = the working tree. CI reads base and head through :func:`collect`.
    """
    verdicts: dict[str, Verdict] = {}
    vdir = root / cfg.verdicts_dir
    for p in sorted(vdir.glob("*.yaml")) if vdir.is_dir() else ():
        xid, v = parse_verdict(yaml.safe_load(p.read_text()), str(p))
        verdicts[xid] = v
    edir = root / cfg.experiments_dir
    specs = {
        p.relative_to(root).as_posix(): yaml.safe_load(p.read_text())
        for p in (sorted(edir.glob("*.yaml")) if edir.is_dir() else ())
    }
    return build_experiments(specs, specs, verdicts, verdicts)[0]


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


def _yaml_files(git: GitRunner, rev: str, directory: str) -> dict[str, Any]:
    """``*.yaml`` directly in *directory* at *rev* (path -> parsed YAML)."""
    names = git(["ls-tree", "--name-only", rev, directory.rstrip("/") + "/"]).splitlines()
    return {n: _show(git, rev, n) for n in sorted(names) if n.endswith(".yaml")}


def _verdicts(git: GitRunner, rev: str, cfg: LaneConfig) -> dict[str, Verdict]:
    out: dict[str, Verdict] = {}
    for path, data in _yaml_files(git, rev, cfg.verdicts_dir).items():
        xid, v = parse_verdict(data, f"{rev[:12]}:{path}")
        out[xid] = v
    return out


@dataclass(frozen=True)
class Diff:
    changed: list[str]
    deltas: dict[str, YamlDelta]
    experiments: dict[str, Experiment]
    removed_specs: list[str]


def collect(git: GitRunner, base: str, head: str, cfg: LaneConfig) -> Diff:
    """Changed paths between the merge base and *head*, leaf deltas per changed
    top-level ``config/<stem>.yaml``, and the experiments (open = on the merge base)."""
    mb = git(["merge-base", base, head]).strip()
    changed = [p for p in git(["diff", "--name-only", mb, head]).splitlines() if p]
    deltas: dict[str, YamlDelta] = {}
    for p in changed:
        m = _CONFIG_YAML_RE.match(p)
        if m is None or m.group(1) == "strategy_lane":
            continue
        stem = m.group(1)
        deltas[stem] = yaml_delta(stem, _show(git, mb, p), _show(git, head, p))
    experiments, removed = build_experiments(
        _yaml_files(git, mb, cfg.experiments_dir),
        _yaml_files(git, head, cfg.experiments_dir),
        _verdicts(git, mb, cfg),
        _verdicts(git, head, cfg),
    )
    return Diff(changed=changed, deltas=deltas, experiments=experiments, removed_specs=removed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Strategy-lane CI check (PLAN D86, card E21.1).")
    ap.add_argument("--base", required=True, help="base commit (PR base sha)")
    ap.add_argument("--head", required=True, help="head commit (PR head sha)")
    ap.add_argument("--body-file", type=Path, required=True, help="PR body text")
    ap.add_argument("--repo", type=Path, default=REPO)
    args = ap.parse_args(argv)
    try:
        cfg = load_lane_config(args.repo / "config" / "strategy_lane.yaml")
        diff = collect(_git(args.repo), args.base, args.head, cfg)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"strategy-lane: config error: {exc}\n")
        return 2
    result = evaluate(
        diff.changed,
        args.body_file.read_text(),
        diff.deltas,
        diff.experiments,
        cfg,
        removed_specs=diff.removed_specs,
    )
    sys.stdout.write(result.render() + "\n")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for line in result.annotations():
            sys.stdout.write(line + "\n")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a") as fh:
                fh.write(result.summary_markdown())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
