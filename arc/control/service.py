"""D26 control panel service: owner-only, bounded, audited config changes.

One service behind both surfaces (``arc config ...`` and the ``!arc`` Slack
subcommands, which shell out to the CLI):

- **Owner-only.** Slack actors must be in ``ARC_APPROVER_SLACK_USER_IDS`` as set
  in the environment (the base settings, not an override, so a narrowed approver
  list can never lock the owner out). Refusals are logged.
- **Bounded.** Every value is parsed by :mod:`arc.control.registry` (type, bounds,
  choices, hard ceiling) and then re-validated as the effective config would load
  it (``ArcSettings`` / the YAML file's own model).
- **Confirm for riskier.** A change in a key's risk direction becomes a pending
  change with a one-time code and a TTL; ``confirm <code>`` applies it. Safer and
  neutral changes apply immediately.
- **Audited.** Every applied change or revert is one append-only
  ``config_changes`` row; its id is the new ``config_version``.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import structlog

from arc.config import ArcSettings
from arc.control.effective import apply_changes, raw_yaml, yaml_overrides
from arc.control.registry import (
    REGISTRY,
    Direction,
    Group,
    Target,
    Tunable,
    TunableError,
    ValueType,
    direction,
    format_value,
    lookup,
    parse_value,
    read_raw,
)
from arc.control.store import ConfigChange, ConfigChangeRepo, PendingChange, PendingRepo, Source

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

__all__ = [
    "CONFIRM_TTL",
    "ControlService",
    "KeyView",
    "NotOwnerError",
    "Result",
]

log = structlog.get_logger(__name__)

CONFIRM_TTL = _dt.timedelta(minutes=10)
LOCAL_ACTOR = "local"


class NotOwnerError(PermissionError):
    """The actor may not change the config."""


@dataclass(frozen=True)
class KeyView:
    """Current state of one key (for ``config show``)."""

    tunable: Tunable
    value: Any
    default: Any
    overridden: bool
    last: ConfigChange | None

    def as_json(self) -> dict[str, Any]:
        t = self.tunable
        return {
            "key": t.key,
            "group": t.group.value,
            "value": self.value,
            "value_text": format_value(t, self.value),
            "default": self.default,
            "default_text": format_value(t, self.default),
            "overridden": self.overridden,
            "bounds": t.bounds,
            "hard_ceiling": t.hard_ceiling,
            "risk": t.risk.value,
            "description": t.description,
            "env": t.env,
            "last_change_id": self.last.id if self.last else None,
            "last_change_at": self.last.at.isoformat() if self.last else None,
            "last_change_by": self.last.actor if self.last else None,
        }


@dataclass
class Result:
    """Outcome of a set / revert / confirm / cancel."""

    outcome: Literal["applied", "reverted", "pending", "cancelled", "unchanged", "refused", "error"]
    key: str | None = None
    message: str = ""
    old: Any = None
    new: Any = None
    direction: str | None = None
    change_id: int | None = None
    config_version: int | None = None
    pending: PendingChange | None = None
    halted: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        t = _maybe(self.key)
        out: dict[str, Any] = {
            "outcome": self.outcome,
            "key": self.key,
            "message": self.message,
            "old": self.old,
            "new": self.new,
            "old_text": format_value(t, self.old) if t else None,
            "new_text": format_value(t, self.new) if t else None,
            "direction": self.direction,
            "change_id": self.change_id,
            "config_version": self.config_version,
            "halted": self.halted,
        }
        if self.pending is not None:
            out["pending"] = {
                "id": self.pending.id,
                "code": self.pending.code,
                "expires_at": self.pending.expires_at.isoformat(),
                "kind": self.pending.kind,
            }
        out.update(self.extra)
        return out


def _maybe(key: str | None) -> Tunable | None:
    if not key:
        return None
    try:
        return lookup(key)
    except TunableError:
        return None


class ControlService:
    """Read and change the effective config. *now* and *optionable* are injectable."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        base: ArcSettings | None = None,
        now: Callable[[], _dt.datetime] | None = None,
        optionable: Callable[[str], bool] | None = None,
        is_halted: Callable[[], bool] | None = None,
        confirm_ttl: _dt.timedelta = CONFIRM_TTL,
    ) -> None:
        from arc.utils.calendar import now_et

        self.conn = conn
        self.base = base if base is not None else ArcSettings()
        self.changes = ConfigChangeRepo(conn)
        self.pending = PendingRepo(conn)
        self._now = now or now_et
        self._optionable = optionable
        self._is_halted = is_halted or self._default_halted
        self.confirm_ttl = confirm_ttl

    # -- identity --------------------------------------------------------------

    def owners(self) -> frozenset[str]:
        return frozenset(u.upper() for u in self.base.approver_slack_user_ids)

    def check_owner(self, actor: str, source: Source) -> None:
        """Slack actors must be approvers; the local CLI (shell access) is the owner."""
        if source == "cli" and actor == LOCAL_ACTOR:
            return
        if actor.upper() not in self.owners():
            log.warning("control.refused_not_owner", actor=actor, source=source)
            msg = f"<@{actor}> is not allowed to change the Arc config (owner only)"
            raise NotOwnerError(msg)

    def _default_halted(self) -> bool:
        from arc.gate.halt import HaltSwitch
        from arc.store.repos import HaltRepo

        try:
            return HaltSwitch(HaltRepo(self.conn)).is_halted()
        except Exception:  # noqa: BLE001 - a flag on the card, never a blocker
            return False

    # -- reads -----------------------------------------------------------------

    def settings(self) -> ArcSettings:
        return apply_changes(self.base, self.changes.active(), version=self.changes.version())

    def version(self) -> int:
        return self.changes.version()

    def _value(self, t: Tunable, settings: ArcSettings) -> Any:
        if t.target is Target.SETTINGS:
            if t.env is not None and t.env != settings.env.value:
                latest = self.changes.latest(t.key)
                if latest is not None and not latest.is_default:
                    return latest.new
                return self._default(t)
            v = getattr(settings, t.field or t.key)
            return list(v) if isinstance(v, list) else v
        raw = raw_yaml(t.target, self._yaml_path(t.target))
        ov = settings.yaml_overrides(t.target.value)
        if ov:
            from arc.utils.yamlpatch import apply_overrides

            raw = apply_overrides(raw, ov)
        return read_raw(t, raw)

    def _default(self, t: Tunable) -> Any:
        if t.target is Target.SETTINGS:
            if t.env is not None and t.env != self.base.env.value:
                return False  # a per-env switch for another env: off unless overridden
            v = getattr(self.base, t.field or t.key)
            return list(v) if isinstance(v, list) else v
        return read_raw(t, raw_yaml(t.target, self._yaml_path(t.target)))

    def _yaml_path(self, target: Target) -> Any:
        if target is Target.PROFILES:
            return self.base.account_profiles_file
        return None

    def view(self, key: str, settings: ArcSettings | None = None) -> KeyView:
        """One key's value; ``settings`` reuses an effective config built once per list."""
        t = lookup(key)
        s = settings if settings is not None else self.settings()
        last = self.changes.latest(t.key)
        return KeyView(
            tunable=t,
            value=self._value(t, s),
            default=self._default(t),
            overridden=last is not None and not last.is_default,
            last=last,
        )

    def keys(self) -> list[str]:
        """Registry keys, each profile's entry DTE window, and overridden pattern keys."""
        profile = self._profile_keys()
        extra = [k for k in self.changes.active() if k not in REGISTRY and k not in profile]
        return [*REGISTRY, *profile, *sorted(extra)]

    def _profile_keys(self) -> list[str]:
        """``profiles.<name>.dte_min|dte_max`` for every profile that sets a window.

        These are the entry DTE window the scanner and gate actually use under
        that profile (the global ``dte_min``/``dte_max`` only apply to a profile
        without a window, i.e. ``margin``), so the summary must show them.
        """
        raw = raw_yaml(Target.PROFILES, self._yaml_path(Target.PROFILES))
        out: list[str] = []
        for name, spec in (raw.get("profiles") or {}).items():
            if not isinstance(spec, dict):
                continue
            for attr in ("dte_min", "dte_max"):
                if spec.get(attr) is not None:
                    out.append(f"profiles.{name}.{attr}")
        return out

    def show(self, what: str | None = None) -> list[KeyView]:
        """Every key, one group, or one key."""
        if what:
            w = what.strip().lower()
            if w in {g.value for g in Group}:
                g = Group(w)
                keys = [k for k in self.keys() if lookup(k).group is g]
                if g is Group.ROUTINES:
                    keys = sorted(set(keys) | set(self._routine_keys()))
                s = self.settings()  # once per list: rebuilding it per key costs ~85 ms
                return [self.view(k, s) for k in keys]
            return [self.view(w)]
        keys = self.keys()
        keys += [k for k in self._routine_keys() if k not in keys]
        s = self.settings()
        return [self.view(k, s) for k in keys]

    def _routine_keys(self) -> list[str]:
        raw = raw_yaml(Target.ROUTINES)
        out: list[str] = []
        for section in ("sources", "personas"):
            for job, spec in (raw.get(section) or {}).items():
                if not isinstance(spec, dict):
                    continue
                out.append(f"routines.{job}.enabled")
                if not spec.get("trigger"):
                    out.append(f"routines.{job}.cadence")
        return out

    def diff(self) -> list[KeyView]:
        """Keys whose effective value differs from the file/env default."""
        return [v for v in (self.view(k) for k in self.changes.active()) if v.value != v.default]

    def history(self, key: str | None = None, *, limit: int = 20) -> list[ConfigChange]:
        k = lookup(key).key if key else None
        return self.changes.history(k, limit=limit)

    # -- validation --------------------------------------------------------------

    def _validate_effective(self, t: Tunable, new: Any, *, is_default: bool = False) -> None:
        """Would the effective config still load with this value? Raises TunableError."""
        active = dict(self.changes.active())
        if is_default:
            active.pop(t.key, None)
        else:
            active[t.key] = ConfigChange(
                id=10**12,
                key=t.key,
                new=new,
                actor="validate",
                at=self._now(),
                source="cli",
                status="applied",
                direction="neutral",
            )
        try:
            s = apply_changes(self.base, active, version=0)
            if t.target is Target.SETTINGS:
                if t.env is None or t.env == s.env.value:
                    got = getattr(s, t.field or t.key)
                    if not is_default and got != new:
                        msg = f"{t.key}: the settings validators rejected {format_value(t, new)}"
                        raise TunableError(msg)
                return
            # YAML targets: load the file's own model with the overrides.
            ov = yaml_overrides(active, profiles_path=self.base.account_profiles_file)
            if t.target is Target.EXITS:
                from arc.exits.policy import load_exit_config

                load_exit_config(overrides=ov.get("exits"))
            elif t.target is Target.COSTS:
                from arc.backtest.costs import load_cost_model

                load_cost_model(overrides=ov.get("costs"))
            elif t.target is Target.ROUTINES:
                from arc.routines.config import load_routines

                load_routines(overrides=ov.get("routines"))
            elif t.target is Target.PROFILES:
                from arc.account_profiles import load_account_profiles

                load_account_profiles(
                    self.base.account_profiles_file, overrides=ov.get("account_profiles")
                )
            if t.target is not Target.ROUTINES and not is_default:
                raw = raw_yaml(t.target, self._yaml_path(t.target))
                from arc.utils.yamlpatch import apply_overrides

                got = read_raw(t, apply_overrides(raw, ov.get(t.target.value)))
                if got != new:  # pragma: no cover - write_raw/read_raw round-trip guard
                    msg = f"{t.key}: override did not round-trip ({got!r} != {new!r})"
                    raise TunableError(msg)
        except TunableError:
            raise
        except ValueError as exc:
            first = str(exc).strip().splitlines()
            detail = first[-1] if first else str(exc)
            msg = f"{t.key}: rejected by the config model: {detail}"
            raise TunableError(msg) from None

    def _check_universe(self, old: list[str], new: list[str]) -> None:
        added = [s for s in new if s not in old]
        if not added or self._optionable is None:
            return
        bad = [s for s in added if not self._optionable(s)]
        if bad:
            msg = f"universe: not optionable: {', '.join(bad)}"
            raise TunableError(msg)

    # -- writes --------------------------------------------------------------------

    def set(
        self,
        key: str,
        raw: str,
        *,
        actor: str,
        source: Source,
        reason: str | None = None,
    ) -> Result:
        """Parse, validate and apply (safer) or stage for confirm (riskier)."""
        try:
            self.check_owner(actor, source)
            t = lookup(key)
            view = self.view(t.key)
            new = parse_value(
                t,
                raw,
                current=view.value,
                base_list=list(self.base.approver_slack_user_ids),
            )
            if t.type is ValueType.TICKERS:
                self._check_universe(list(view.value or []), list(new))
            self._validate_effective(t, new)
        except NotOwnerError as exc:
            return Result("refused", key=key, message=str(exc))
        except TunableError as exc:
            log.info("control.rejected", key=key, actor=actor, error=str(exc))
            return Result("refused", key=key, message=str(exc))
        return self._stage_or_apply(
            t,
            old=view.value,
            new=new,
            is_default=False,
            kind="set",
            actor=actor,
            source=source,
            reason=reason,
        )

    def set_system(self, key: str, raw: str, *, actor: str, reason: str) -> Result:
        """E6.6a: a change made by Arc itself (``arc:*`` actor), safer direction only.

        Used for pre-registered automatic transitions (e.g. turning the E7.5a
        scorecard gate back on when the collection phase ends). A riskier or
        unchanged value is refused / a no-op; nothing is staged for confirm. Logged
        like any other change (``config_changes``, source ``cli``).
        """
        if not actor.startswith("arc:"):
            return Result("refused", key=key, message=f"{actor!r} is not a system actor")
        try:
            t = lookup(key)
            view = self.view(t.key)
            new = parse_value(t, raw, current=view.value, base_list=[])
            self._validate_effective(t, new)
        except TunableError as exc:
            return Result("refused", key=key, message=str(exc))
        d = direction(t, view.value, new)
        if d is Direction.RISKIER:
            return Result(
                "refused",
                key=t.key,
                old=view.value,
                new=new,
                direction=d.value,
                message=f"{t.key}: a system actor may only make safer changes",
            )
        return self._stage_or_apply(
            t,
            old=view.value,
            new=new,
            is_default=False,
            kind="set",
            actor=actor,
            source="cli",
            reason=reason,
        )

    def revert(
        self,
        ref: str,
        *,
        actor: str,
        source: Source,
        reason: str | None = None,
    ) -> Result:
        """Undo a change: ``<change_id>`` restores that change's ``old``; ``<key>`` resets
        the key to its file/env default. Riskier reverts need a confirm too."""
        try:
            self.check_owner(actor, source)
            ref = ref.strip()
            supersedes: int | None
            if ref.isdigit():
                target = self.changes.get(int(ref))
                if target is None:
                    raise TunableError(f"no config change #{ref}")
                t = lookup(target.key)
                supersedes = target.id
                prior = self._prior_override(target)
                if prior is None:
                    is_default, new = True, self._default(t)
                else:
                    is_default, new = prior.is_default, prior.new
                    if is_default:
                        new = self._default(t)
            else:
                t = lookup(ref)
                latest = self.changes.latest(t.key)
                if latest is None or latest.is_default:
                    return Result("unchanged", key=t.key, message=f"{t.key} is already default")
                supersedes = latest.id
                is_default, new = True, self._default(t)
            old = self.view(t.key).value
            self._validate_effective(t, new, is_default=is_default)
        except NotOwnerError as exc:
            return Result("refused", key=ref, message=str(exc))
        except TunableError as exc:
            return Result("refused", key=ref, message=str(exc))
        return self._stage_or_apply(
            t,
            old=old,
            new=new,
            is_default=is_default,
            kind="revert",
            actor=actor,
            source=source,
            reason=reason,
            supersedes_id=supersedes,
        )

    def _prior_override(self, change: ConfigChange) -> ConfigChange | None:
        """The row for the same key just before *change* (what it replaced)."""
        row = self.conn.execute(
            "SELECT id FROM config_changes WHERE key = ? AND id < ? ORDER BY id DESC LIMIT 1",
            (change.key, change.id),
        ).fetchone()
        return self.changes.get(int(row[0])) if row else None

    def _stage_or_apply(
        self,
        t: Tunable,
        *,
        old: Any,
        new: Any,
        is_default: bool,
        kind: Literal["set", "revert"],
        actor: str,
        source: Source,
        reason: str | None,
        supersedes_id: int | None = None,
    ) -> Result:
        d = direction(t, old, new)
        if d is Direction.UNCHANGED and not (kind == "revert" and supersedes_id is not None):
            return Result(
                "unchanged",
                key=t.key,
                old=old,
                new=new,
                message=f"{t.key} is already {format_value(t, new)}",
            )
        if d is Direction.RISKIER:
            p = self.pending.create(
                key=t.key,
                old=old,
                new=new,
                is_default=is_default,
                kind=kind,
                actor=actor,
                reason=reason,
                source=source,
                now=self._now(),
                ttl=self.confirm_ttl,
                base_version=self.version(),
                supersedes_id=supersedes_id,
            )
            log.info("control.pending", key=t.key, code=p.code, actor=actor)
            return Result(
                "pending",
                key=t.key,
                old=old,
                new=new,
                direction=d.value,
                pending=p,
                halted=self._is_halted(),
                message=(
                    f"riskier change: confirm with `confirm {p.code}` within "
                    f"{int(self.confirm_ttl.total_seconds() // 60)} min"
                ),
            )
        return self._apply(
            t,
            old=old,
            new=new,
            is_default=is_default,
            kind=kind,
            actor=actor,
            source=source,
            reason=reason,
            direction_=d,
            supersedes_id=supersedes_id,
        )

    def _apply(
        self,
        t: Tunable,
        *,
        old: Any,
        new: Any,
        is_default: bool,
        kind: Literal["set", "revert"],
        actor: str,
        source: Source,
        reason: str | None,
        direction_: Direction,
        supersedes_id: int | None,
        pending_id: str | None = None,
    ) -> Result:
        halted = self._is_halted()
        row = self.changes.append(
            key=t.key,
            old=old,
            new=new,
            is_default=is_default,
            actor=actor,
            reason=reason,
            at=self._now(),
            source=source,
            status="applied" if kind == "set" else "reverted",
            direction=(
                Direction.NEUTRAL.value if direction_ is Direction.UNCHANGED else direction_.value
            ),
            supersedes_id=supersedes_id,
            halted=halted,
            pending_id=pending_id,
        )
        log.info(
            "control.applied",
            key=t.key,
            change_id=row.id,
            actor=actor,
            kind=kind,
            direction=row.direction,
            halted=halted,
        )
        return Result(
            "applied" if kind == "set" else "reverted",
            key=t.key,
            old=old,
            new=new,
            direction=row.direction,
            change_id=row.id,
            config_version=row.id,
            halted=halted,
            message="applies at the next tick",
        )

    def confirm(self, code: str, *, actor: str, source: Source) -> Result:
        try:
            self.check_owner(actor, source)
        except NotOwnerError as exc:
            return Result("refused", message=str(exc))
        now = self._now()
        self.pending.expire_due(now)
        p = self.pending.by_code(code)
        if p is None:
            return Result("refused", message=f"no pending change with code {code!r}")
        if p.resolved_at is not None:
            return Result("refused", key=p.key, message=f"code {p.code} is already {p.outcome}")
        t = lookup(p.key)
        current = self.view(t.key).value
        if current != p.old:
            self.pending.resolve(p.id, outcome="cancelled", by=f"stale:{actor}", now=now)
            return Result(
                "refused",
                key=p.key,
                message=(
                    f"{p.key} changed since the request ({format_value(t, p.old)} -> "
                    f"{format_value(t, current)}); run the set again"
                ),
            )
        new = self._default(t) if p.is_default else p.new
        try:
            self._validate_effective(t, new, is_default=p.is_default)
        except TunableError as exc:
            self.pending.resolve(p.id, outcome="cancelled", by=f"invalid:{actor}", now=now)
            return Result("refused", key=p.key, message=str(exc))
        if not self.pending.resolve(p.id, outcome="confirmed", by=actor, now=now):
            return Result("refused", key=p.key, message=f"code {p.code} was already resolved")
        return self._apply(
            t,
            old=p.old,
            new=new,
            is_default=p.is_default,
            kind=p.kind,
            actor=p.actor if p.actor == actor else f"{p.actor}+{actor}",
            source=source,
            reason=p.reason,
            direction_=Direction.RISKIER,
            supersedes_id=p.supersedes_id,
            pending_id=p.id,
        )

    def cancel(self, code: str, *, actor: str, source: Source) -> Result:
        try:
            self.check_owner(actor, source)
        except NotOwnerError as exc:
            return Result("refused", message=str(exc))
        p = self.pending.by_code(code)
        if p is None or p.resolved_at is not None:
            return Result("refused", message=f"no open pending change with code {code!r}")
        self.pending.resolve(p.id, outcome="cancelled", by=actor, now=self._now())
        return Result("cancelled", key=p.key, old=p.old, new=p.new, message="cancelled")
