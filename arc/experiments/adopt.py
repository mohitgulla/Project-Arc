"""``arc experiment adopt``: an owner-approved Analyst proposal becomes a registered XP
(E21.4, PLAN D86).

The one way a new experiment enters the registry. Deterministic, idempotent per ``P-<n>``:

1. read the proposal (an ``xp_backlog.json`` entry, or the Analyst's approved
   ``proposals/P-<n>.yaml``), pick the next free ``XP-<n>`` across the DB, the specs
   committed in the working tree and ``origin/main`` (:func:`next_free_id`);
2. fill the defaults from ``config/experiments.yaml`` and validate: v2 spec, ``kind: ab``,
   exactly :data:`K_TREATMENTS` treatments, every overlay target loads, the arms fit
   ``experiments.runner.max_parallel_arms``;
3. ``create`` + ``register`` with ``proposal_ref: P-<n>`` (``registered``, or ``queued``
   with the reason when its area or the arm cap is busy);
4. commit ``config/experiments/live/xp<n>_<slug>.yaml`` through an auto-merged PR on
   branch ``xp/<n>`` (:mod:`arc.experiments.publish`);
5. one line in the #arc-investor day thread.

A re-run for the same ``P-<n>`` finds the experiment by its ``proposal_ref`` and only
retries what is missing (register, the PR, nothing else). ``--dry-run`` runs 1-3 on an
in-memory copy of the store and prints the file, branch, PR text and Slack line.
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from arc.experiments.models import (
    ExperimentKind,
    ExperimentSpec,
    ExperimentState,
    ExperimentStatus,
    spec_hash,
)
from arc.experiments.publish import EXPERIMENTS_DIR, VERDICTS_DIR
from arc.experiments.store import (
    PROPOSAL_REF_RE,
    ExperimentError,
    ExperimentStore,
    check_owner_approval,
    proposal_ref_of,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from arc.experiments.config import ExperimentsConfig
    from arc.experiments.publish import MainView, Publisher, PublishResult

__all__ = [
    "ADOPT_ACTOR",
    "K_TREATMENTS",
    "AdoptError",
    "AdoptResult",
    "Proposal",
    "adopt",
    "adopt_pr_body",
    "load_proposal",
    "next_free_id",
    "spec_file_text",
    "spec_path",
    "start_estimate",
]

log = structlog.get_logger(__name__)

ADOPT_ACTOR = "arc.adopt"
K_TREATMENTS = 4  # D86: control + exactly 4 treatments
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_XP_NUM_RE = re.compile(r"\bXP-([1-9]\d*)\b")
_TERMINAL = frozenset(
    {ExperimentStatus.STOPPED, ExperimentStatus.PROMOTED, ExperimentStatus.REJECTED}
)
#: the Analyst's placeholder id in an approved ``proposals/P-<n>.yaml``
_PLACEHOLDER_IDS = frozenset({"XP-<next>", "XP-<n>", ""})
AUTOSTART_AT = _dt.time(9, 5)  # config/routines.yaml experiments.autostart


class AdoptError(ExperimentError):
    """The proposal cannot be adopted (bad input, wrong shape, already concluded)."""


@dataclass(frozen=True)
class Proposal:
    """What adopt needs from an Analyst proposal: its id and the spec fields."""

    proposal_id: str
    spec: dict[str, Any]
    proposed_by: str = "owner"


@dataclass
class AdoptResult:
    experiment_id: str
    proposal_id: str
    status: str
    created: bool
    area: str
    arms: int
    spec_hash: str
    path: str
    branch: str
    queued_behind: list[str] = field(default_factory=list)
    queue_reason: str | None = None
    starts: str | None = None  # ISO date of the earliest pre-open auto-start
    pr_url: str | None = None
    pr_status: str | None = None
    slack: str = ""
    dry_run: bool = False
    file_text: str = ""
    pr_title: str = ""
    pr_body: str = ""

    def as_json(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        if not self.dry_run:
            out.pop("file_text")
        return out


# -- input ------------------------------------------------------------------------------


def load_proposal(data: Mapping[str, Any], *, proposal_id: str | None = None) -> Proposal:
    """A :class:`Proposal` from a backlog entry (``{"id": "P-<n>", "spec": {...}}``) or an
    approved spec document (``proposals/P-<n>.yaml``; *proposal_id* names it)."""
    if isinstance(data.get("spec"), dict):  # an xp_backlog.json entry
        pid = str(data.get("id") or proposal_id or "")
        if proposal_id and pid != proposal_id:
            msg = f"the backlog entry is {pid}, not {proposal_id}"
            raise AdoptError(msg)
        status = data.get("status")
        if status not in (None, "approved", "registered", "running"):
            msg = f"{pid} is {status}: only an owner-approved proposal is adopted"
            raise AdoptError(msg)
        spec = dict(data["spec"])
        by = str(data.get("finding_id") or spec.pop("proposed_by", None) or "owner")
    else:
        pid = proposal_id or ""
        spec = dict(data)
        by = str(spec.pop("proposed_by", None) or "owner")
    if not PROPOSAL_REF_RE.match(pid):
        msg = f"proposal id {pid!r} must look like P-<n> (pass it, or name the file P-<n>.yaml)"
        raise AdoptError(msg)
    if str(spec.get("id", "")) not in _PLACEHOLDER_IDS:
        msg = f"{pid}: the proposal already carries id {spec['id']!r}; adopt assigns it"
        raise AdoptError(msg)
    spec.pop("id", None)
    return Proposal(proposal_id=pid, spec=spec, proposed_by=by)


def read_proposal_file(path: Path | str, proposal_id: str | None = None) -> Proposal:
    """:func:`load_proposal` of a YAML/JSON file; a ``P-<n>.yaml`` name gives the id."""
    p = Path(path)
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        msg = f"{p}: top level must be a mapping"
        raise AdoptError(msg)
    if proposal_id is None and PROPOSAL_REF_RE.match(p.stem):
        proposal_id = p.stem
    return load_proposal(data, proposal_id=proposal_id)


# -- ids, paths, text -------------------------------------------------------------------


def _xp_numbers(texts: Iterable[str]) -> set[int]:
    return {int(m) for t in texts for m in _XP_NUM_RE.findall(t)}


def committed_texts(root: Path) -> list[str]:
    """Names and contents of every committed experiment file in the working tree."""
    out: list[str] = []
    for d in (EXPERIMENTS_DIR, VERDICTS_DIR, f"{EXPERIMENTS_DIR}/history"):
        base = root / d
        for p in sorted(base.glob("*")) if base.is_dir() else ():
            if p.is_file() and p.suffix in (".yaml", ".json"):
                out += [p.name, _ids_only(p.read_text())]
    return out


def _ids_only(text: str) -> str:
    """The ``id`` / ``experiment_id`` values of a file (comments may cite other ids)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return ""
    if not isinstance(data, dict):
        return ""
    return " ".join(str(data.get(k, "")) for k in ("id", "experiment_id"))


def main_texts(view: MainView) -> list[str]:
    out: list[str] = []
    for d in (EXPERIMENTS_DIR, VERDICTS_DIR, f"{EXPERIMENTS_DIR}/history"):
        for name, text in view.files(d).items():
            out += [Path(name).name, _ids_only(text)]
    return out


def next_free_id(store: ExperimentStore, *namespaces: Iterable[str]) -> str:
    """``XP-<max+1>`` over the DB and every namespace of names / ids (no reuse, ever)."""
    used = {int(s.experiment_id.split("-")[1]) for s in store.all()}
    for ns in namespaces:
        used |= _xp_numbers(ns)
        used |= {int(m) for t in ns for m in re.findall(r"\bxp([1-9]\d*)[_.]", t)}
    return f"XP-{max(used, default=0) + 1}"


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return s[:40].rstrip("_") or "experiment"


def spec_path(spec: ExperimentSpec) -> str:
    n = spec.id.split("-")[1]
    return f"{EXPERIMENTS_DIR}/xp{n}_{_slug(spec.title)}.yaml"


def spec_doc(spec: ExperimentSpec) -> dict[str, Any]:
    return spec.model_dump(mode="json", exclude_none=True)


def spec_file_text(spec: ExperimentSpec, *, proposal_id: str, approval: str) -> str:
    """The committed spec: header + YAML that re-parses to the same locked hash."""
    h = spec_hash(spec)
    head = (
        f"# {spec.id}: adopted from Arc Analyst proposal {proposal_id} (owner approval "
        f"{approval}),\n"
        "# registered by `arc experiment adopt` (E21.4, PLAN D86). Hash-locked: edits need a\n"
        f"# new experiment id. spec sha256 {h}\n"
    )
    body = yaml.safe_dump(spec_doc(spec), sort_keys=False, allow_unicode=True, width=88)
    text = head + body
    again = ExperimentSpec.model_validate(yaml.safe_load(text))
    if spec_hash(again) != h:  # pragma: no cover - guards a serialisation change
        msg = f"{spec.id}: the spec file does not re-parse to the locked hash"
        raise AdoptError(msg)
    return text


def adopt_pr_body(st: ExperimentState, proposal_id: str, approval: str) -> str:
    return (
        f"Lane: fast — register {st.experiment_id} (owner approved {proposal_id}, PLAN D86)\n"
        "XP-advisory: none — this PR is the experiment's registered spec\n\n"
        f"`arc experiment adopt` registered **{st.experiment_id}** ({st.spec.title}) from "
        f"Analyst proposal {proposal_id}, owner approval `{approval}`.\n\n"
        f"- area `{st.spec.area.value}`, {len(st.spec.arms.treatments)} treatments\n"
        f"- status `{st.status.value}`\n"
        f"- spec sha256 `{st.registered_hash}` (the pre-registration lock)\n\n"
        "Auto-merged once CI is green. `experiments.autostart` starts it at the first "
        "pre-open after this merge when its area and arms are free (E21.4).\n"
    )


def start_estimate(now: _dt.datetime, status: ExperimentStatus) -> _dt.date | None:
    """The first pre-open the auto-start can pick it up (None while queued)."""
    from arc.utils.calendar import ET, is_session, next_session

    if status is not ExperimentStatus.REGISTERED:
        return None
    local = now.astimezone(ET)
    today = local.date()
    if is_session(today) and local.time() < AUTOSTART_AT:
        return today
    return next_session(today)


def slack_line(st: ExperimentState, proposal_id: str, starts: _dt.date | None) -> str:
    k = len(st.spec.arms.treatments)
    head = f"{st.experiment_id} registered ({proposal_id}), {k} arms, area {st.spec.area.value}"
    if starts is not None:
        return f"{head}, starts {starts:%a %b} {starts.day} pre-open"
    behind = _queued_detail(st)[0]
    return f"{head}, queued behind {', '.join(behind) or 'the arm cap'}"


def _queued_detail(st: ExperimentState) -> tuple[list[str], str | None]:
    q = next((e for e in reversed(st.events) if e.status is ExperimentStatus.QUEUED), None)
    if q is None:
        return [], None
    behind = [str(x) for x in q.detail.get("queued_behind") or []]
    reason = q.detail.get("arm_cap") or (
        f"area {st.spec.area.value} held by {', '.join(behind)}" if behind else None
    )
    return behind, reason


# -- the operation ----------------------------------------------------------------------


def find_adopted(store: ExperimentStore, proposal_id: str) -> list[ExperimentState]:
    return [s for s in store.all() if proposal_ref_of(s) == proposal_id]


def _build_spec(proposal: Proposal, experiment_id: str, cfg: ExperimentsConfig) -> ExperimentSpec:
    from pydantic import ValidationError

    from arc.experiments.overlay import fill_defaults, validate_arms

    data = {
        "spec_version": 2,
        "kind": "ab",
        **proposal.spec,
        "id": experiment_id,
        "proposed_by": proposal.proposed_by,
    }
    try:
        spec = ExperimentSpec.model_validate(data)
        validate_arms(spec)
    except (ValidationError, ValueError) as exc:
        msg = f"{proposal.proposal_id}: invalid spec: {exc}"
        raise AdoptError(msg) from exc
    if spec.spec_version != 2:
        msg = f"{proposal.proposal_id}: adopt takes a v2 spec (arms.treatments t1..t{K_TREATMENTS})"
        raise AdoptError(msg)
    if spec.kind is not ExperimentKind.AB:
        msg = f"{proposal.proposal_id}: adopt registers ab experiments only, not {spec.kind.value}"
        raise AdoptError(msg)
    k = len(spec.arms.treatments)
    if k != K_TREATMENTS:
        msg = (
            f"{proposal.proposal_id}: {k} treatment(s); D86 proposals are control + exactly "
            f"{K_TREATMENTS} treatments (FOUR-TREATMENTS)"
        )
        raise AdoptError(msg)
    return fill_defaults(spec, cfg.defaults)


def _memory_copy(conn: sqlite3.Connection) -> sqlite3.Connection:
    mem = sqlite3.connect(":memory:")
    conn.backup(mem)
    mem.row_factory = conn.row_factory
    return mem


def adopt(
    conn: sqlite3.Connection,
    proposal: Proposal,
    cfg: ExperimentsConfig,
    *,
    owner_approval: str,
    now: _dt.datetime,
    main: MainView | None = None,
    publisher: Publisher | None = None,
    repo: Path = REPO_ROOT,
    dry_run: bool = False,
    post: Any = None,
) -> AdoptResult:
    """Adopt *proposal* (see the module doc). *main* = ``origin/main`` for the id scan
    (None: skip it); *publisher* opens the spec PR (None: skip); *post(text)* writes the
    Slack line (None: skip). ``dry_run`` changes nothing (in-memory store copy)."""
    approval = check_owner_approval(owner_approval)
    db = _memory_copy(conn) if dry_run else conn
    store = ExperimentStore.for_runner(db, cfg.runner, now=lambda: now)
    pid = proposal.proposal_id
    found = find_adopted(store, pid)
    live = [s for s in found if s.status not in _TERMINAL]
    created = False
    if live:
        st = live[-1]
    elif found:
        ids = ", ".join(s.experiment_id for s in found)
        msg = f"{pid} was already adopted as {ids}, now concluded; a re-run needs a new proposal"
        raise AdoptError(msg)
    else:
        namespaces: list[list[str]] = [committed_texts(repo)]
        if main is not None:
            namespaces.append(main_texts(main))
        xid = next_free_id(store, *namespaces)
        spec = _build_spec(proposal, xid, cfg)
        need = store.arms_of(spec)
        cap = cfg.runner.max_parallel_arms
        if need > cap:
            msg = f"{xid} runs {need} arms, more than max_parallel_arms {cap}: it could never start"
            raise AdoptError(msg)
        st = store.create(spec, actor=ADOPT_ACTOR, owner_approval=approval, proposal_ref=pid)
        created = True
    if st.status is ExperimentStatus.DRAFT:
        st = store.register(
            st.experiment_id, actor=ADOPT_ACTOR, owner_approval=approval, proposal_ref=pid
        )
    behind, reason = _queued_detail(st) if st.status is ExperimentStatus.QUEUED else ([], None)
    starts = start_estimate(now, st.status)
    path = spec_path(st.spec)
    branch = f"xp/{st.experiment_id.split('-')[1]}"
    text = spec_file_text(st.spec, proposal_id=pid, approval=approval)
    title = f"{st.experiment_id}: register {st.spec.title} (owner approved {pid}, D86)"
    body = adopt_pr_body(st, pid, approval)
    line = slack_line(st, pid, starts)
    res = AdoptResult(
        experiment_id=st.experiment_id,
        proposal_id=pid,
        status=st.status.value,
        created=created,
        area=st.spec.area.value,
        arms=len(st.spec.arms.treatments),
        spec_hash=st.registered_hash or st.spec_hash,
        path=path,
        branch=branch,
        queued_behind=behind,
        queue_reason=reason,
        starts=None if starts is None else starts.isoformat(),
        slack=line,
        dry_run=dry_run,
        file_text=text,
        pr_title=title,
        pr_body=body,
    )
    if dry_run:
        db.close()
        return res
    if post is not None and created:  # once per P-<n>: before the PR, which may fail
        post(line)
    if publisher is not None:
        pr: PublishResult = publisher.publish(
            branch=branch, files={path: text}, title=title, body=body
        )
        res.pr_url, res.pr_status = pr.url, pr.status
        from arc.routines.runs import RoutineStateRepo

        if pr.url:
            RoutineStateRepo(conn).set(f"xp_spec_pr:{st.experiment_id}", pr.url, now=now)
    log.info(
        "experiments.adopted",
        experiment_id=st.experiment_id,
        proposal=pid,
        status=st.status.value,
        created=created,
        pr=res.pr_url,
    )
    return res
