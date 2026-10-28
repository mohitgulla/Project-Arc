"""``arc backtest study adopt BT-<n>`` (card E26.2, PLAN D121): commit an owner-approved
Analyst draft study and queue it for the nightly runner (E26.3).

Mirrors ``arc experiment adopt`` (E21.4):

1. the owner approval must be ``slack:<ts>`` (the owner's ``approve BT-<n>`` message in
   #arc-analyst, the D86 rule extended to BT);
2. the draft is validated as an E7.12 :class:`~arc.backtest.study.StudySpec` (every arm's
   overlay on the base config) and hash-locked: the committed file re-parses to the same
   spec hash, ``--expect-hash`` (the hash an earlier attempt recorded) must still match,
   and a re-approval under a queued id must carry the queued hash;
3. an id already in ``config/backtest_studies/`` (locally or on ``origin/main``) or in the
   research ledger is refused, unless it is this same approval being retried;
4. the spec is committed on branch ``bt/<n>`` through an auto-merged PR
   (:class:`arc.experiments.publish.GitPublisher`);
5. a ``queued`` entry is appended to ``data/backtest/queue.json``
   (:mod:`arc.backtest.queue`); the runner skips it until the PR has merged (E26.3).

Idempotent per BT id: re-running after a failed PR or queue step retries that step; the
publisher reuses an open or merged PR for the branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from arc.backtest.queue import (
    APPROVAL_RE,
    WEEKLY_APPROVAL_CAP,
    QueueEntry,
    QueueError,
    check_weekly_cap,
    eta_rows,
    load_queue,
    rung_estimates,
    update_queue,
)
from arc.backtest.study import (
    RETIRED_FILE,
    STUDIES_DIR,
    RetiredStudy,
    StudySpec,
    load_ledger,
    load_retired,
    load_study,
    retired_from_text,
    retired_message,
    study_hash,
)

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Mapping
    from pathlib import Path

    from arc.experiments.publish import MainView, Publisher

__all__ = [
    "SPECS_DIR",
    "AdoptStudyError",
    "AdoptStudyResult",
    "adopt_study",
    "check_not_retired",
    "spec_file_text",
    "study_pr_body",
    "weekly_cap_or_raise",
]

log = structlog.get_logger(__name__)

SPECS_DIR = "config/backtest_studies"


class AdoptStudyError(RuntimeError):
    """The adoption was refused; nothing was committed or queued."""


@dataclass
class AdoptStudyResult:
    id: str
    spec_hash: str
    status: str  # the queue status (queued | running | ...), "dry-run" when nothing is written
    created: bool
    path: str
    branch: str
    pr_title: str
    pr_body: str
    file_text: str
    pr_url: str | None = None
    pr_status: str | None = None  # opened | existing | merged | on-main | dry-run
    auto_merge: bool | None = None
    queue_position: int | None = None
    eta_nights: int | None = None
    compute_s: int | None = None
    rung_estimates: dict[str, Any] = field(default_factory=dict)
    entry: dict[str, Any] | None = None
    dry_run: bool = False
    dry_run_pr: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "spec_hash": self.spec_hash,
            "status": self.status,
            "created": self.created,
            "path": self.path,
            "branch": self.branch,
            "pr_url": self.pr_url,
            "pr_status": self.pr_status,
            "auto_merge": self.auto_merge,
            "queue_position": self.queue_position,
            "eta_nights": self.eta_nights,
            "compute_s": self.compute_s,
            "rung_estimates": self.rung_estimates,
            "entry": self.entry,
            "dry_run": self.dry_run,
            "dry_run_pr": self.dry_run_pr,
            "pr_title": self.pr_title,
        }


def spec_file_text(spec: StudySpec, *, approval: str) -> str:
    """The committed spec: a header + YAML that re-parses to the same locked hash."""
    h = study_hash(spec)
    head = (
        f"# {spec.id}: Arc Analyst draft study, committed by `arc backtest study adopt` on the\n"
        f"# owner's approval {approval} (E26.2, PLAN D121). Hash-locked: an edit is a new\n"
        f"# spec hash (and a new variant). spec sha256 {h}\n"
    )
    doc = spec.model_dump(mode="json", exclude_defaults=True)
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=88)
    text = head + body
    again = StudySpec.model_validate(yaml.safe_load(text))
    if study_hash(again) != h:  # pragma: no cover - guards a serialisation change
        msg = f"{spec.id}: the spec file does not re-parse to the locked hash"
        raise AdoptStudyError(msg)
    return text


def study_pr_body(spec: StudySpec, h: str, approval: str) -> str:
    return (
        f"XP-advisory: none — {spec.id} is an offline backtest study spec (E26.2, PLAN D121)\n\n"
        f"`arc backtest study adopt` commits **{spec.id}** ({spec.hypothesis}), drafted by "
        f"{spec.owner}, on the owner's approval `{approval}` in #arc-analyst.\n\n"
        f"- profiles {', '.join(spec.profiles)}; control + {len(spec.arms.treatments)} "
        f"treatment(s); primary {spec.primary_metric}; max_variants {spec.max_variants}\n"
        f"- dev {spec.period.dev_start} -> {spec.period.dev_end}; holdout "
        f"{spec.period.holdout_start} -> {spec.period.holdout_end or 'latest'}\n"
        f"- spec sha256 `{h}` (the pre-registration lock)\n\n"
        "Auto-merged once CI is green. The nightly study runner (E26.3) runs it after the "
        "merge: c125 dev -> c500 dev -> one c500 holdout, stopping early on a clear dev "
        "fail. Nothing here changes trading config.\n"
    )


def _spec_hash_of(text: str, where: str) -> str:
    try:
        data = yaml.safe_load(text) or {}
        return study_hash(StudySpec.model_validate(data))
    except (ValueError, yaml.YAMLError) as exc:
        msg = f"{where} is not a loadable study spec: {exc}"
        raise AdoptStudyError(msg) from exc


def _committed(study_id: str, studies_dir: Path, main: MainView | None) -> list[tuple[str, str]]:
    """``(where, spec hash)`` of every committed file for *study_id*: the local spec dir
    and ``origin/main``'s."""
    out: list[tuple[str, str]] = []
    local = studies_dir / f"{study_id}.yaml"
    if local.is_file():
        out.append((str(local), _spec_hash_of(local.read_text(), str(local))))
    if main is not None:
        path = f"{SPECS_DIR}/{study_id}.yaml"
        text = main.files(SPECS_DIR).get(path)
        if text is not None:
            out.append((f"origin/main:{path}", _spec_hash_of(text, f"origin/main:{path}")))
    return out


def _retired(studies_dir: Path, main: MainView | None) -> dict[str, RetiredStudy]:
    """E26.5 (D127): retired studies of the local spec dir and ``origin/main`` (union)."""
    out = dict(load_retired(studies_dir))
    if main is not None:
        path = f"{SPECS_DIR}/{RETIRED_FILE}"
        text = main.files(SPECS_DIR).get(path)
        out = {**retired_from_text(text, f"origin/main:{path}"), **out}
    return out


def check_not_retired(study_id: str, retired: Mapping[str, RetiredStudy], *, queued: bool) -> None:
    """Refuse a retired id, and (ids are never reused) a new id at or below the highest
    retired one unless it is already queued (a retry of an earlier adoption)."""
    prefix = study_id.split("-", 1)[0]
    r = retired.get(study_id)
    if r is not None:
        msg = f"{retired_message(r)}; a new study takes a new {prefix} id"
        raise AdoptStudyError(msg)
    top = max((int(k.split("-", 1)[1]) for k in retired), default=0)
    if not queued and int(study_id.split("-", 1)[1]) <= top:
        msg = (
            f"{study_id} is at or below the highest retired id {prefix}-{top} (D127: ids are "
            f"never reused); the Analyst re-drafts under {prefix}-{top + 1} or above"
        )
        raise AdoptStudyError(msg)


def weekly_cap_or_raise(
    queue_path: Path, study_id: str, now: dt.datetime, cap: int = WEEKLY_APPROVAL_CAP
) -> None:
    """The shared D121/D133 weekly approval cap (BT + ML) as an :class:`AdoptStudyError`."""
    try:
        check_weekly_cap(load_queue(queue_path), study_id, now, cap)
    except QueueError as exc:
        raise AdoptStudyError(str(exc)) from exc


def adopt_study(  # noqa: PLR0913
    study_id: str,
    spec_path: Path,
    *,
    approval: str,
    now: dt.datetime,
    queue_path: Path,
    ledger: Path,
    studies_dir: Path | None = None,
    main: MainView | None = None,
    publisher: Publisher | None = None,
    expect_hash: str | None = None,
    dry_run: bool = False,
    dry_run_pr: bool = False,
    paths: Mapping[str, Path | None] | None = None,
    weekly_cap: int = WEEKLY_APPROVAL_CAP,
) -> AdoptStudyResult:
    """Adopt the draft at *spec_path* as *study_id* (see the module doc). ``dry_run``
    writes nothing; ``dry_run_pr`` skips only the PR (the queue entry is written).
    A new id is refused once *now*'s ISO week holds *weekly_cap* approvals, BT and ML
    together (D121, D133; :func:`arc.backtest.queue.check_weekly_cap`)."""
    approval = (approval or "").strip()
    if not APPROVAL_RE.match(approval):
        msg = (
            f"invalid approval {approval!r}: a study adoption needs the owner's Slack "
            "approval, --approval slack:<ts> (the `approve BT-<n>` message, D86/D121)"
        )
        raise AdoptStudyError(msg)
    if not spec_path.is_file():
        msg = f"no draft spec {spec_path}"
        raise AdoptStudyError(msg)
    if spec_path.stem != study_id:
        msg = f"{spec_path}: the draft's file name must be {study_id}.yaml"
        raise AdoptStudyError(msg)
    try:
        spec = load_study(spec_path, paths=paths)
    except ValueError as exc:
        msg = f"{study_id}: the draft does not validate as a StudySpec: {exc}"
        raise AdoptStudyError(msg) from exc
    if spec.id != study_id:  # pragma: no cover - load_study ties the id to the file name
        msg = f"{spec_path}: id {spec.id} != {study_id}"
        raise AdoptStudyError(msg)
    h = study_hash(spec)
    if expect_hash and expect_hash != h:
        msg = (
            f"{study_id}: hash mismatch: the draft is {h[:12]} but the approval was recorded "
            f"for {expect_hash[:12]} (the draft changed since); a changed design needs a new "
            "BT id"
        )
        raise AdoptStudyError(msg)

    queued = load_queue(queue_path).get(study_id)
    try:
        retired = _retired(studies_dir or STUDIES_DIR, main)
    except ValueError as exc:
        msg = f"cannot read the retired-study list: {exc}"
        raise AdoptStudyError(msg) from exc
    check_not_retired(study_id, retired, queued=queued is not None)
    if queued is not None and queued.spec_hash != h:
        msg = (
            f"{study_id}: hash mismatch: queued with spec {queued.spec_hash[:12]} "
            f"({queued.status}), this draft is {h[:12]}; a changed design needs a new BT id"
        )
        raise AdoptStudyError(msg)
    if queued is None:
        rows = [e for e in load_ledger(ledger) if e.study == study_id]
        if rows:
            msg = (
                f"{study_id} is taken: the research ledger already has {len(rows)} run(s) of "
                "it; the Analyst re-drafts under the next free BT id"
            )
            raise AdoptStudyError(msg)
    for where, other in _committed(study_id, studies_dir or STUDIES_DIR, main):
        if other != h:
            msg = (
                f"{study_id} is taken: {where} holds spec {other[:12]}, not this draft's "
                f"{h[:12]}; the Analyst re-drafts under the next free BT id"
            )
            raise AdoptStudyError(msg)
    weekly_cap_or_raise(queue_path, study_id, now, weekly_cap)

    text = spec_file_text(spec, approval=approval)
    path = f"{SPECS_DIR}/{study_id}.yaml"
    branch = f"bt/{study_id.split('-')[1]}"
    title = f"{study_id}: commit backtest study spec (owner approved, D121)"
    body = study_pr_body(spec, h, approval)
    res = AdoptStudyResult(
        id=study_id,
        spec_hash=h,
        status="dry-run" if dry_run else "queued",
        created=False,
        path=path,
        branch=branch,
        pr_title=title,
        pr_body=body,
        file_text=text,
        dry_run=dry_run,
        dry_run_pr=dry_run_pr,
        rung_estimates=rung_estimates(load_ledger(ledger)),
    )
    if dry_run:
        res.pr_status = "dry-run"
        return res

    # 1. the spec PR (auto-merge). A failure leaves the queue untouched: retry re-sends.
    pr_url = queued.pr_url if queued is not None else None
    if dry_run_pr or publisher is None:
        res.pr_status = "dry-run" if dry_run_pr else "skipped"
    else:
        pr = publisher.publish(branch=branch, files={path: text}, title=title, body=body)
        res.pr_url, res.pr_status, res.auto_merge = pr.url, pr.status, pr.auto_merge
        pr_url = pr.url or pr_url

    # 2. the queue entry (idempotent per id + hash)
    entry = QueueEntry(id=study_id, spec_hash=h, approved_at=now, approval=approval, pr_url=pr_url)

    def _add(q: Any) -> tuple[QueueEntry, bool]:
        from arc.backtest.queue import enqueue

        try:
            check_weekly_cap(q, study_id, now, weekly_cap)  # re-checked under the lock
            return enqueue(q, entry)
        except QueueError as exc:  # pragma: no cover - checked above, re-checked under lock
            raise AdoptStudyError(str(exc)) from exc

    got, created = update_queue(queue_path, _add)
    res.created = created
    res.status = got.status
    res.pr_url = res.pr_url or got.pr_url
    res.entry = got.model_dump(mode="json")
    rows = {r["id"]: r for r in eta_rows(load_queue(queue_path), load_ledger(ledger))}
    row = rows.get(study_id) or {}
    res.queue_position = row.get("queue_position")
    res.eta_nights = row.get("eta_nights")
    res.compute_s = row.get("compute_s")
    log.info(
        "backtest.study.adopted",
        study=study_id,
        spec_hash=h[:12],
        created=created,
        pr=res.pr_url,
        position=res.queue_position,
    )
    return res
