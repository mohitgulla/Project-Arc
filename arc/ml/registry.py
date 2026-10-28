"""The ML model registry (E27.3, D126/D133): ``data/ml/models/<family>/<version>/``.

Layout per family::

    data/ml/models/<family>/
      models.json            index: {"family", "champion", "versions": {v: {...}}}
      promotions.jsonl       append-only audit of every status change (config_changes style)
      holdout_reads.jsonl    append-only log of every holdout read of this family
      <version>/manifest.json, files listed in manifest["files"] with sha256

Rules:

* ``candidate → shadow`` is automatic when the version's pre-registered promote rule
  passed (:func:`register` with ``rule_passed=True``).
* ``shadow → champion`` is owner-only (:func:`promote`, ``actor`` must equal
  ``ArcSettings.owner_slack_user_id``); the previous champion becomes ``retired``.
* A holdout read (:func:`authorize_holdout_read`) is allowed once per family; every
  later read needs the owner acknowledgement :data:`HOLDOUT_REREAD_ACK` (the text of
  the owner's ``OWNER UPDATE: holdout re-read ok`` card comment) and the owner actor.
* :func:`verify` re-hashes every file of a version and raises on any mismatch.

Machine-readable JSON only (the Tower reads it with the stdlib, E27.9). No LightGBM
import here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pathlib import Path

Status = Literal["candidate", "shadow", "champion", "retired"]
STATUSES: tuple[str, ...] = ("candidate", "shadow", "champion", "retired")
HOLDOUT_REREAD_ACK = "OWNER UPDATE: holdout re-read ok"
INDEX = "models.json"
PROMOTIONS = "promotions.jsonl"
HOLDOUT_LOG = "holdout_reads.jsonl"


class RegistryError(RuntimeError):
    """A refused registry operation (bad transition, non-owner actor, hash mismatch)."""


class HoldoutReadRefusedError(RegistryError):
    """A second holdout read of a family without the owner's re-read acknowledgement."""


class VersionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Status = "candidate"
    created: str
    rule_passed: bool | None = None
    rule: str | None = Field(None, description="Registered promote-rule kind, e.g. m2_promote")
    feature_schema: str | None = None
    summary: dict[str, object] = Field(default_factory=dict)


class FamilyIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family: str
    champion: str | None = None
    versions: dict[str, VersionEntry] = Field(default_factory=dict)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_files(vdir: Path, names: list[str]) -> dict[str, str]:
    return {n: sha256_file(vdir / n) for n in sorted(names)}


def _index_path(root: Path, family: str) -> Path:
    return root / family / INDEX


def load_index(root: Path, family: str) -> FamilyIndex:
    p = _index_path(root, family)
    if not p.is_file():
        return FamilyIndex(family=family)
    return FamilyIndex.model_validate_json(p.read_text())


def _save_index(root: Path, idx: FamilyIndex) -> None:
    p = _index_path(root, idx.family)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
    tmp.replace(p)


def _append(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def verify(root: Path, family: str, version: str) -> dict[str, str]:
    """Re-hash every file the version's manifest lists; raise :class:`RegistryError` on
    a missing or changed file. Returns the manifest's ``files`` map."""
    vdir = root / family / version
    man_p = vdir / "manifest.json"
    if not man_p.is_file():
        msg = f"{family}/{version}: no manifest.json"
        raise RegistryError(msg)
    files: dict[str, str] = json.loads(man_p.read_text()).get("files", {})
    for name, want in files.items():
        p = vdir / name
        if not p.is_file():
            msg = f"{family}/{version}: {name} missing"
            raise RegistryError(msg)
        got = sha256_file(p)
        if got != want:
            msg = f"{family}/{version}: {name} sha256 {got[:12]} != manifest {want[:12]}"
            raise RegistryError(msg)
    return files


def register(
    root: Path,
    family: str,
    version: str,
    *,
    now: dt.datetime,
    rule: str,
    rule_passed: bool,
    feature_schema: str | None = None,
    summary: dict[str, object] | None = None,
    auto_shadow: bool = True,
) -> VersionEntry:
    """Add (or refresh) *version* as ``candidate``; ``shadow`` when the rule passed.

    ``auto_shadow=False`` (the monthly ``ml.retrain``, D140) registers ``candidate``
    whatever the rule said: promotion stays a manual owner step. Re-registering a
    version keeps a ``champion``/``retired`` status (never demotes).
    """
    verify(root, family, version)
    idx = load_index(root, family)
    prev = idx.versions.get(version)
    status: Status = "shadow" if rule_passed and auto_shadow else "candidate"
    if prev is not None and prev.status in ("champion", "retired"):
        status = prev.status
    entry = VersionEntry(
        status=status,
        created=prev.created if prev else now.isoformat(),
        rule=rule,
        rule_passed=rule_passed,
        feature_schema=feature_schema,
        summary=summary or {},
    )
    idx.versions[version] = entry
    _save_index(root, idx)
    if prev is None or prev.status != status:
        _append(
            root / family / PROMOTIONS,
            {
                "at": now.isoformat(),
                "family": family,
                "version": version,
                "old": None if prev is None else prev.status,
                "new": status,
                "actor": "registry",
                "reason": f"{rule} {'passed' if rule_passed else 'failed'}",
            },
        )
    return entry


def check_promotable(root: Path, family: str, version: str, *, actor: str, owner: str) -> None:
    """Refuse unless *actor* is the owner and *version* is intact and either an E27.3
    ``shadow`` with a fixed holdout read, or (D140) a monthly-retrain ``candidate``
    whose newest rolling-holdout read passed."""
    if actor != owner:
        msg = f"{actor!r} is not the owner; only {owner} may promote a model to champion"
        raise RegistryError(msg)
    verify(root, family, version)
    entry = load_index(root, family).versions.get(version)
    if entry is None:
        msg = f"{family}/{version} is not registered"
        raise RegistryError(msg)
    rolling = [r for r in rolling_reads(root, family) if r.get("version") == version]
    if entry.status == "candidate" and rolling:
        if not rolling[-1].get("passed"):
            msg = f"{family}/{version}: its rolling-holdout gate failed; not promotable (D140)"
            raise RegistryError(msg)
        return
    if entry.status != "shadow":
        msg = (
            f"{family}/{version} is {entry.status}; only a shadow version (or a retrain "
            "candidate that passed its rolling holdout, D140) can become champion"
        )
        raise RegistryError(msg)
    if not any(r.get("version") == version for r in holdout_reads(root, family)):
        msg = (
            f"{family}/{version} has no holdout read; run `arc ml m2 holdout` first "
            "(a re-read needs the owner's ack, D133)"
        )
        raise RegistryError(msg)


def promote(
    root: Path,
    family: str,
    version: str,
    *,
    actor: str,
    owner: str,
    now: dt.datetime,
    reason: str = "",
) -> FamilyIndex:
    """Owner-only ``shadow → champion`` (or a D140 retrain ``candidate``); the previous
    champion is ``retired``."""
    check_promotable(root, family, version, actor=actor, owner=owner)
    idx = load_index(root, family)
    entry = idx.versions[version]
    was = entry.status
    old = idx.champion
    if old is not None and old in idx.versions:
        idx.versions[old].status = "retired"
        _append(
            root / family / PROMOTIONS,
            {
                "at": now.isoformat(),
                "family": family,
                "version": old,
                "old": "champion",
                "new": "retired",
                "actor": actor,
                "reason": f"replaced by {version}",
            },
        )
    entry.status = "champion"
    idx.champion = version
    _save_index(root, idx)
    _append(
        root / family / PROMOTIONS,
        {
            "at": now.isoformat(),
            "family": family,
            "version": version,
            "old": was,
            "new": "champion",
            "actor": actor,
            "reason": reason,
        },
    )
    return idx


# ---------------------------------------------------------------------------
# Point-in-time model selection (E27.5, D126): the backtest's only way to a model
# ---------------------------------------------------------------------------

#: Sessions between a fold's last training label and its test block: M1 purges rows
#: dated before ``sessions[idx(b) − h − 5]`` (labels end ≤ 5 sessions before the block,
#: :mod:`arc.ml.m1.train`); M2 purges on ``label_end_date`` with a 10-session embargo
#: (:func:`arc.ml.validate.embargo_start`).
LABEL_EMBARGO: dict[str, int] = {"m1": 5}


class ModelAsOf(BaseModel):
    """The model a sim session may use (:func:`model_asof`).

    ``labels_before``: every training label of this model ended strictly before this
    date (purge + embargo), and ``labels_before <= serves_from <= d`` for every session
    *d* it serves. ``fold`` is the walk-forward fold dir (``test2022``) or ``None`` for
    the version's final fit; ``path`` the model directory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    family: str
    version: str
    fold: str | None
    path: str
    labels_before: dt.date
    serves_from: dt.date
    serves_to: dt.date | None = Field(None, description="None = open-ended (final fit)")


def pick_version(root: Path, family: str, version: str | None = None) -> str:
    """*version*, else the family's newest non-retired registered version (``created``,
    then name); a family without an index (M1) falls back to its last version dir by
    name. Never the champion pointer: a backtest names its version or takes the newest."""
    if version is not None:
        if not (root / family / version / "manifest.json").is_file():
            msg = f"{family}/{version}: no manifest.json under {root}"
            raise RegistryError(msg)
        return version
    idx = load_index(root, family)
    live = [(e.created, v) for v, e in idx.versions.items() if e.status != "retired"]
    if live:
        return max(live)[1]
    dirs = sorted(p.parent.name for p in (root / family).glob("*/manifest.json"))
    if not dirs:
        msg = f"no {family} model under {root}"
        raise RegistryError(msg)
    return dirs[-1]


def asof_table(
    root: Path, family: str, sessions: list[dt.date], version: str | None = None
) -> list[ModelAsOf]:
    """Every model of one version with the session block it may serve, oldest first.

    A fold ``test<Y>`` serves its own test year (as E27.3 scored it out of sample); the
    final fit serves from the day after its train window (and after the last fold).
    ``labels_before`` is the fold's purge bound (:data:`LABEL_EMBARGO`).
    """
    from arc.ml import validate as v

    ver = pick_version(root, family, version)
    vdir = root / family / ver
    man = json.loads((vdir / "manifest.json").read_text())
    folds = man.get("folds") or [f"test{y}" for y in v.TEST_YEARS]
    emb = LABEL_EMBARGO.get(family, v.EMBARGO)
    out: list[ModelAsOf] = []
    last_end: dt.date | None = None
    for name in sorted(folds):
        y = int(str(name).removeprefix("test"))
        start, end = dt.date(y, 1, 1), dt.date(y, 12, 31)
        lb = v.embargo_start(sessions, start, emb)
        fdir = vdir / "folds" / str(name)
        out.append(
            ModelAsOf(
                family=family,
                version=ver,
                fold=str(name),
                path=str(fdir if fdir.is_dir() else vdir),
                labels_before=min(lb, start),
                serves_from=start,
                serves_to=end,
            )
        )
        last_end = end if last_end is None else max(last_end, end)
    tw = man.get("train_window") or {}
    if tw.get("end"):
        lb = dt.date.fromisoformat(str(tw["end"])) + dt.timedelta(days=1)
        frm = lb if last_end is None else max(lb, last_end + dt.timedelta(days=1))
        out.append(
            ModelAsOf(
                family=family,
                version=ver,
                fold=None,
                path=str(vdir),
                labels_before=lb,
                serves_from=frm,
                serves_to=None,
            )
        )
    return out


def model_asof(
    root: Path,
    family: str,
    d: dt.date,
    *,
    sessions: list[dt.date] | None = None,
    version: str | None = None,
    table: list[ModelAsOf] | None = None,
) -> ModelAsOf | None:
    """The walk-forward model a sim session *d* may use: trained only on labels that
    ended before *d* (``None`` = no such model, e.g. before the first test year).

    Pass *table* (:func:`asof_table`) to look up many sessions without re-reading the
    manifest. The backtest's ML arms read models only through this function.
    """
    if table is None:
        if sessions is None:
            from arc.utils.calendar import sessions_between

            sessions = sessions_between(dt.date(2019, 1, 1), max(d, dt.date(2024, 12, 31)))
        table = asof_table(root, family, sessions, version)
    hits = [
        m
        for m in table
        if m.serves_from <= d and (m.serves_to is None or d <= m.serves_to) and m.labels_before <= d
    ]
    return max(hits, key=lambda m: m.labels_before) if hits else None


#: ``kind`` of a monthly rolling-holdout read (D140); the fixed D84 holdout reads have
#: no ``kind`` (or ``fixed``).
ROLLING = "rolling"


def holdout_reads(root: Path, family: str) -> list[dict[str, object]]:
    """The family's fixed (D84 2024+) holdout reads; rolling reads are kept apart."""
    return [r for r in _read_jsonl(root / family / HOLDOUT_LOG) if r.get("kind") != ROLLING]


def rolling_reads(root: Path, family: str) -> list[dict[str, object]]:
    """The family's monthly rolling-holdout reads (``ml.retrain``, D140)."""
    return [r for r in _read_jsonl(root / family / HOLDOUT_LOG) if r.get("kind") == ROLLING]


def log_rolling_read(root: Path, family: str, row: dict[str, object]) -> None:
    """Append one rolling-holdout read (window, gap, n, metrics) to ``holdout_reads.jsonl``.

    D140: the rolling holdout is a pass/fail gate read once per monthly run; it never
    counts toward the one-time fixed-holdout read of :func:`authorize_holdout_read`.
    """
    _append(root / family / HOLDOUT_LOG, {**row, "family": family, "kind": ROLLING})


def authorize_holdout_read(
    root: Path,
    family: str,
    version: str,
    *,
    now: dt.datetime,
    code_sha: str,
    reason: str,
    actor: str | None = None,
    owner: str | None = None,
    ack: str | None = None,
) -> int:
    """Log one holdout read of *family*; return the read count after it.

    The first read of a family needs only a *reason*. Any later read raises
    :class:`HoldoutReadRefusedError` unless *ack* is exactly :data:`HOLDOUT_REREAD_ACK`
    and *actor* is the *owner* (D126 / D133).
    """
    if not reason:
        msg = "a holdout read needs a reason (D84)"
        raise HoldoutReadRefusedError(msg)
    prior = holdout_reads(root, family)
    if prior and (ack != HOLDOUT_REREAD_ACK or owner is None or actor != owner):
        msg = (
            f"{family} holdout already read {len(prior)}x (last {prior[-1].get('at')}); "
            f"a re-read needs the owner's '{HOLDOUT_REREAD_ACK}' (D126)"
        )
        raise HoldoutReadRefusedError(msg)
    _append(
        root / family / HOLDOUT_LOG,
        {
            "at": now.isoformat(),
            "family": family,
            "version": version,
            "sha": code_sha,
            "reason": reason,
            "actor": actor or "card",
            "n": len(prior) + 1,
        },
    )
    return len(prior) + 1
