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

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    import datetime as dt
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
) -> VersionEntry:
    """Add (or refresh) *version* as ``candidate``; ``shadow`` when the rule passed.

    Re-registering a version keeps a ``champion``/``retired`` status (never demotes).
    """
    verify(root, family, version)
    idx = load_index(root, family)
    prev = idx.versions.get(version)
    status: Status = "shadow" if rule_passed else "candidate"
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
    """Refuse unless *actor* is the owner and *version* is an intact ``shadow``."""
    if actor != owner:
        msg = f"{actor!r} is not the owner; only {owner} may promote a model to champion"
        raise RegistryError(msg)
    verify(root, family, version)
    entry = load_index(root, family).versions.get(version)
    if entry is None:
        msg = f"{family}/{version} is not registered"
        raise RegistryError(msg)
    if entry.status != "shadow":
        msg = f"{family}/{version} is {entry.status}; only a shadow version can become champion"
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
    """Owner-only ``shadow → champion``; the previous champion is ``retired``."""
    check_promotable(root, family, version, actor=actor, owner=owner)
    idx = load_index(root, family)
    entry = idx.versions[version]
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
            "old": "shadow",
            "new": "champion",
            "actor": actor,
            "reason": reason,
        },
    )
    return idx


def holdout_reads(root: Path, family: str) -> list[dict[str, object]]:
    return _read_jsonl(root / family / HOLDOUT_LOG)


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
