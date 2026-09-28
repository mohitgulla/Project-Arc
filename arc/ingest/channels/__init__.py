"""Channel processor registry (E4.4, PLAN D14).

Every sub-directory of ``arc/ingest/channels/`` holding a ``profile.yaml`` and
a ``GUIDELINES.md`` is a channel processor; adding a channel needs no code.
``default/`` is the fallback for channels without their own profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import structlog

from arc.ingest.channels.base import ChannelProcessor, ChannelProfile

log = structlog.get_logger()

CHANNELS_DIR = Path(__file__).resolve().parent
DEFAULT_SLUG = "default"


@dataclass(frozen=True)
class ChannelRegistry:
    """Maps YouTube channel ids and slugs to processors."""

    by_channel_id: dict[str, ChannelProcessor]
    by_slug: dict[str, ChannelProcessor]
    default: ChannelProcessor

    @classmethod
    def load(cls, *roots: Path) -> ChannelRegistry:
        """Load every ``<root>/<slug>/profile.yaml``; later roots override earlier.

        The built-in ``default/`` processor is used when no root provides one.
        """
        roots = roots or (CHANNELS_DIR,)
        by_slug: dict[str, ChannelProcessor] = {}
        for root in roots:
            for profile in sorted(root.glob("*/profile.yaml")):
                proc = ChannelProcessor.from_dir(profile.parent)
                by_slug[proc.profile.slug] = proc
        default = by_slug.get(DEFAULT_SLUG) or ChannelProcessor.from_dir(
            CHANNELS_DIR / DEFAULT_SLUG
        )
        by_slug.setdefault(DEFAULT_SLUG, default)
        by_channel_id: dict[str, ChannelProcessor] = {}
        for proc in by_slug.values():
            cid = proc.profile.channel_id
            if cid:
                if cid in by_channel_id:
                    msg = f"duplicate channel_id {cid} in {proc.profile.slug}"
                    raise ValueError(msg)
                by_channel_id[cid] = proc
        return cls(by_channel_id=by_channel_id, by_slug=by_slug, default=default)

    def for_channel(self, channel_id: str | None) -> ChannelProcessor:
        """Processor for *channel_id*, or the default processor if unknown."""
        if channel_id and channel_id in self.by_channel_id:
            return self.by_channel_id[channel_id]
        log.info("channel.registry.default", channel_id=channel_id)
        return self.default

    def for_slug(self, slug: str) -> ChannelProcessor | None:
        return self.by_slug.get(slug)

    @property
    def profiles(self) -> list[ChannelProfile]:
        return [p.profile for p in self.by_slug.values()]


@lru_cache(maxsize=1)
def default_registry() -> ChannelRegistry:
    """The registry of built-in channels (cached; profiles are read-only)."""
    return ChannelRegistry.load(CHANNELS_DIR)


__all__ = [
    "CHANNELS_DIR",
    "DEFAULT_SLUG",
    "ChannelProcessor",
    "ChannelProfile",
    "ChannelRegistry",
    "default_registry",
]
