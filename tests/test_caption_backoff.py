"""Tests for YouTube caption rate-limit handling (E4.1c, D15).

No network: ``urlopen`` / ``_download_subtitle`` and yt-dlp are faked; sleep and
the jitter RNG are injected.
"""

from __future__ import annotations

import io
import json
import random
import sqlite3
import urllib.error
import wave
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

from arc.config import ArcSettings
from arc.ingest.caption_backoff import (
    BACKOFF_KEY,
    CaptionBackoff,
    CaptionResult,
    CaptionStatus,
    cooldown_minutes,
    is_sorry_page,
    load_backoff,
    parse_retry_after,
    register_rate_limit,
    save_backoff,
)
from arc.ingest.store import IngestCursorRepo
from arc.ingest.transcribe import FixtureTranscriber
from arc.ingest.youtube import _download_subtitle, fetch_youtube
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

NOW = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)
FFMPEG = "/fake/ffmpeg"
SORRY_HTML = (
    "<html><head><title>Sorry...</title></head><body>"
    "<p>Our systems have detected unusual traffic from your computer network.</p>"
    '<a href="https://www.google.com/sorry/index">more</a></body></html>'
)


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


CHANNELS = ["https://www.youtube.com/@T"]


def _settings(**kw: object) -> ArcSettings:
    base: dict[str, object] = {
        "env": "paper",
        "universe": ["SPY"],
        "ffmpeg_bin": FFMPEG,
        "yt_caption_sleep_seconds": 7.5,
    }
    base.update(kw)
    return ArcSettings(**base)  # type: ignore[arg-type]


@pytest.fixture()
def settings() -> ArcSettings:
    return _settings()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _info(vid: str, *, age_min: float = 120, duration: float = 600) -> dict:
    ts = NOW - timedelta(minutes=age_min)
    return {
        "id": vid,
        "title": f"Title {vid}",
        "channel": "StockedUp",
        "upload_date": ts.strftime("%Y%m%d"),
        "timestamp": int(ts.timestamp()),
        "duration": duration,
        "subtitles": {},
        "automatic_captions": {"en-orig": [{"ext": "vtt", "url": f"https://yt/tt?v={vid}"}]},
    }


def _tools(infos: dict[str, dict]):
    def run(cmd: list[str], **_kw: object) -> mock.MagicMock:
        r = mock.MagicMock(returncode=0, stdout="", stderr="")
        if "--flat-playlist" in cmd:
            r.stdout = "\n".join(json.dumps({"id": v}) for v in infos)
        elif "--dump-single-json" in cmd:
            r.stdout = json.dumps(infos[cmd[-1].rsplit("=", 1)[-1]])
        elif "bestaudio" in cmd:
            Path(cmd[cmd.index("-o") + 1].replace("%(ext)s", "m4a")).write_bytes(b"x")
        elif cmd[0] == FFMPEG:
            with wave.open(cmd[-1], "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16_000)
                w.writeframes(b"\x00\x00" * 1600)
        return r

    return run


def _fetch(db, settings, infos, results, *, now=NOW, rng=None, sleep=None, tx=None):
    """Run fetch_youtube with ``_download_subtitle`` returning ``results`` in order."""
    sleep = sleep or mock.Mock()
    tx = tx or FixtureTranscriber(text="spy audio words")
    with (
        mock.patch("subprocess.run", side_effect=_tools(infos)),
        mock.patch("arc.ingest.youtube._download_subtitle", side_effect=list(results)) as dl,
        mock.patch("arc.ingest.youtube.resolve_ffmpeg", return_value=FFMPEG),
        capture_logs() as logs,
    ):
        docs = fetch_youtube(
            db,
            settings,
            channels=CHANNELS,
            transcriber=tx,
            now=now,
            rng=rng or random.Random(0),
            sleep=sleep,
        )
    return docs, dl, sleep, tx, logs


def _events(logs: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    return [e for e in logs if e["event"] == name]


RL = CaptionResult(CaptionStatus.RATE_LIMITED, http_status=429)
OK = CaptionResult.ok("spy caption words")


# ---------------------------------------------------------------------------
# Classification (_download_subtitle)
# ---------------------------------------------------------------------------


def _http_error(code: int, body: str = "", headers: dict[str, str] | None = None):
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://yt/tt?v=x", code, "err", msg, io.BytesIO(body.encode()))


def _ok_response(body: bytes, *, url: str = "https://yt/tt?v=x", status: int = 200):
    resp = mock.MagicMock()
    inner = resp.__enter__.return_value
    inner.read.return_value = body
    inner.status = status
    inner.geturl.return_value = url
    return resp


class TestClassification:
    def test_429_is_rate_limited(self) -> None:
        err = _http_error(429, SORRY_HTML)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            res = _download_subtitle("https://yt/tt?v=x")
        assert res.status is CaptionStatus.RATE_LIMITED
        assert res.http_status == 429
        assert res.retry_after_s is None

    def test_retry_after_captured(self) -> None:
        err = _http_error(429, "", {"Retry-After": "7200"})
        with mock.patch("urllib.request.urlopen", side_effect=err):
            res = _download_subtitle("https://yt/tt?v=x")
        assert res.status is CaptionStatus.RATE_LIMITED
        assert res.retry_after_s == 7200

    def test_sorry_page_on_other_status_is_rate_limited(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(503, SORRY_HTML)):
            assert _download_subtitle("u").status is CaptionStatus.RATE_LIMITED

    def test_sorry_page_with_200_is_rate_limited(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_ok_response(SORRY_HTML.encode())):
            assert _download_subtitle("u").status is CaptionStatus.RATE_LIMITED

    def test_redirect_to_sorry_is_rate_limited(self) -> None:
        resp = _ok_response(b"<html></html>", url="https://www.google.com/sorry/index?continue=x")
        with mock.patch("urllib.request.urlopen", return_value=resp):
            assert _download_subtitle("u").status is CaptionStatus.RATE_LIMITED

    def test_empty_200_is_empty(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_ok_response(b"")):
            res = _download_subtitle("u")
        assert res.status is CaptionStatus.EMPTY
        assert res.http_status == 200

    def test_header_only_vtt_is_empty(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_ok_response(b"WEBVTT\n\n")):
            assert _download_subtitle("u").status is CaptionStatus.EMPTY

    def test_other_http_error_is_error(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(404, "not found")):
            res = _download_subtitle("u")
        assert res.status is CaptionStatus.ERROR
        assert res.http_status == 404

    def test_network_error_is_error(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            res = _download_subtitle("u")
        assert res.status is CaptionStatus.ERROR
        assert "timed out" in res.error

    def test_caption_text_mentioning_unusual_traffic_is_ok(self) -> None:
        vtt = b"WEBVTT\n\n00:00.000 --> 00:01.000\nunusual traffic on the sorry index\n"
        with mock.patch("urllib.request.urlopen", return_value=_ok_response(vtt)):
            res = _download_subtitle("u")
        assert res.status is CaptionStatus.OK

    def test_is_sorry_page(self) -> None:
        assert is_sorry_page(SORRY_HTML)
        assert not is_sorry_page("<html><body>fine</body></html>")
        assert not is_sorry_page("plain unusual traffic text")
        assert is_sorry_page("", "https://www.google.com/sorry/index")

    def test_parse_retry_after(self) -> None:
        assert parse_retry_after(None, now=NOW) is None
        assert parse_retry_after("120", now=NOW) == 120
        assert parse_retry_after("Mon, 28 Sep 2026 03:00:00 GMT", now=NOW) == 3600
        assert parse_retry_after("Mon, 28 Sep 2026 01:00:00 GMT", now=NOW) == 0
        assert parse_retry_after("garbage", now=NOW) is None


# ---------------------------------------------------------------------------
# Logging + per-run breaker
# ---------------------------------------------------------------------------


class TestRunBehaviour:
    def test_429_logs_rate_limited_not_download_failed(self, db, settings) -> None:
        *_, logs = _fetch(db, settings, {"v1": _info("v1")}, [RL])
        (ev,) = _events(logs, "youtube.captions_rate_limited")
        assert ev["log_level"] == "warning"
        assert ev["video_id"] == "v1"
        assert ev["status"] == 429
        assert ev["retry_after"] is False
        assert _events(logs, "youtube.caption_download_failed") == []

    def test_empty_logs_captions_empty(self, db, settings) -> None:
        empty = CaptionResult(CaptionStatus.EMPTY, http_status=200)
        docs, *_, logs = _fetch(db, settings, {"v1": _info("v1")}, [empty])
        (ev,) = _events(logs, "youtube.captions_empty")
        assert ev["video_id"] == "v1" and ev["status"] == 200
        assert _events(logs, "youtube.caption_download_failed") == []
        assert _events(logs, "youtube.captions_rate_limited") == []
        assert load_backoff(db) == CaptionBackoff()  # empty never starts a cooldown
        assert docs[0].transcript_source == "audio"

    def test_real_error_logs_download_failed(self, db, settings) -> None:
        err = CaptionResult(CaptionStatus.ERROR, http_status=500, error="boom")
        *_, logs = _fetch(db, settings, {"v1": _info("v1")}, [err])
        (ev,) = _events(logs, "youtube.caption_download_failed")
        assert ev["status"] == 500 and ev["error"] == "boom"

    def test_breaker_stops_further_timedtext_calls(self, db) -> None:
        settings = _settings(yt_max_audio_per_run=5)
        infos = {v: _info(v) for v in ("v1", "v2", "v3", "v4")}
        docs, dl, _, tx, logs = _fetch(db, settings, infos, [OK, RL])
        assert dl.call_count == 2  # v1 ok, v2 429, v3/v4 never requested
        skipped = _events(logs, "youtube.captions_skipped")
        assert [e["video_id"] for e in skipped] == ["v3", "v4"]
        assert {e["reason"] for e in skipped} == {"breaker"}
        assert [d.transcript_source for d in docs] == ["captions", "audio", "audio", "audio"]
        assert len(tx.calls) == 3

    def test_rate_limited_videos_use_audio_within_run_cap(self, db) -> None:
        settings = _settings(yt_max_audio_per_run=2)
        infos = {v: _info(v) for v in ("v1", "v2", "v3", "v4")}
        docs, dl, _, tx, logs = _fetch(db, settings, infos, [RL])
        assert dl.call_count == 1
        assert len(tx.calls) == 2
        assert [d.url[-2:] for d in docs] == ["v1", "v2"]
        reasons = [e["reason"] for e in _events(logs, "youtube.audio_skipped")]
        assert reasons == ["run_cap_reached", "run_cap_reached"]

    def test_rate_limited_young_video_respects_grace(self, db, settings) -> None:
        docs, _, _, tx, logs = _fetch(db, settings, {"v1": _info("v1", age_min=5)}, [RL])
        assert docs == [] and tx.calls == []
        (ev,) = _events(logs, "youtube.audio_skipped")
        assert ev["reason"] == "within_caption_grace"

    def test_sleep_between_requests_uses_configured_value(self, db) -> None:
        settings = _settings(yt_caption_sleep_seconds=7.5)
        infos = {v: _info(v) for v in ("v1", "v2", "v3")}
        _, dl, sleep, *_ = _fetch(db, settings, infos, [OK, OK, OK])
        assert dl.call_count == 3
        assert sleep.call_args_list == [mock.call(7.5), mock.call(7.5)]  # not before the first

    def test_no_sleep_when_breaker_open(self, db, settings) -> None:
        infos = {v: _info(v) for v in ("v1", "v2", "v3")}
        _, _, sleep, *_ = _fetch(db, settings, infos, [RL])
        sleep.assert_not_called()

    def test_run_stats_report_caption_audio_and_cooldown(self, db) -> None:
        """E5.3: every run's YouTube outcome is summarised (owner note on E4.1c)."""
        from arc.ingest.youtube import YoutubeRunStats

        settings = _settings(yt_max_audio_per_run=2)
        infos = {v: _info(v) for v in ("v1", "v2", "v3", "v4")}
        stats = YoutubeRunStats()
        with (
            mock.patch("subprocess.run", side_effect=_tools(infos)),
            mock.patch("arc.ingest.youtube._download_subtitle", side_effect=[OK, RL]),
            mock.patch("arc.ingest.youtube.resolve_ffmpeg", return_value=FFMPEG),
        ):
            fetch_youtube(
                db,
                settings,
                channels=CHANNELS,
                transcriber=FixtureTranscriber(text="spy audio words"),
                now=NOW,
                rng=random.Random(0),
                sleep=mock.Mock(),
                stats=stats,
            )
        assert stats.captions == {"ok": 1, "rate_limited": 1}
        assert (stats.captions_skipped, stats.skip_reason) == (2, "breaker")
        assert stats.audio == 2 and stats.audio_failed == 0
        assert stats.no_transcript == 1  # v4: audio run cap reached
        assert stats.consecutive_rate_limits == 1
        assert stats.cooldown_until == load_backoff(db).cooldown_until
        text = stats.summary()
        assert "captions: ok 1, rate_limited 1, empty 0, error 0, skipped 2 (breaker)" in text
        assert "audio 2 (" in text and "1 without transcript yet" in text
        assert "captions cooldown until" in text and "(streak 1)" in text

    def test_run_stats_cooldown_skip_and_none(self, db, settings) -> None:
        from arc.ingest.youtube import YoutubeRunStats

        _fetch(db, settings, {"v1": _info("v1")}, [RL])  # opens a cooldown
        stats = YoutubeRunStats()
        with (
            mock.patch("subprocess.run", side_effect=_tools({"v2": _info("v2")})),
            mock.patch("arc.ingest.youtube._download_subtitle") as dl,
            mock.patch("arc.ingest.youtube.resolve_ffmpeg", return_value=FFMPEG),
        ):
            fetch_youtube(
                db,
                settings,
                channels=CHANNELS,
                transcriber=FixtureTranscriber(text="spy"),
                now=NOW + timedelta(minutes=5),
                stats=stats,
            )
        dl.assert_not_called()
        assert (stats.captions_skipped, stats.skip_reason) == (1, "cooldown")
        assert stats.captions == {} and stats.audio == 1
        assert stats.cooldown_until is not None
        assert YoutubeRunStats().summary().endswith("captions cooldown: none")


# ---------------------------------------------------------------------------
# Cross-run cooldown
# ---------------------------------------------------------------------------


class TestCooldown:
    def test_cooldown_persisted_and_honoured_next_run(self, db, settings) -> None:
        _fetch(db, settings, {"v1": _info("v1")}, [RL])
        state = load_backoff(db)
        assert state.consecutive_rate_limits == 1
        assert state.cooldown_until is not None
        assert state.cooldown_until.tzinfo is not None
        assert state.cooldown_until.utcoffset() == NOW.astimezone(ET).utcoffset()  # ET
        assert IngestCursorRepo(db).get(BACKOFF_KEY)  # stored in the DB

        later = NOW + timedelta(minutes=10)
        docs, dl, _, tx, logs = _fetch(db, settings, {"v2": _info("v2")}, [], now=later)
        dl.assert_not_called()
        (ev,) = _events(logs, "youtube.captions_cooldown_active")
        assert ev["until"] == state.cooldown_until.isoformat()
        assert docs[0].transcript_source == "audio"

    def test_cooldown_expired_retries_captions_and_success_resets(self, db, settings) -> None:
        save_backoff(
            db,
            CaptionBackoff(
                cooldown_until=(NOW - timedelta(minutes=1)).astimezone(ET),
                consecutive_rate_limits=3,
            ),
        )
        docs, dl, *_, logs = _fetch(db, settings, {"v1": _info("v1")}, [OK])
        assert dl.call_count == 1
        assert docs[0].transcript_source == "captions"
        assert load_backoff(db) == CaptionBackoff()
        assert _events(logs, "youtube.captions_backoff_reset")

    def test_backoff_doubles_across_runs_and_caps(self, db) -> None:
        settings = _settings(
            yt_caption_cooldown_base_minutes=30,
            yt_caption_cooldown_max_minutes=100,
            yt_caption_cooldown_jitter=0,
        )
        now = NOW
        lengths = []
        for i in range(4):
            _fetch(db, settings, {f"v{i}": _info(f"v{i}")}, [RL], now=now)
            state = load_backoff(db)
            assert state.consecutive_rate_limits == i + 1
            assert state.cooldown_until is not None
            lengths.append((state.cooldown_until - now).total_seconds() / 60)
            now = state.cooldown_until + timedelta(seconds=1)  # next run after expiry
        assert lengths == pytest.approx([30, 60, 100, 100])

    def test_force_audio_does_not_touch_backoff(self, db, settings) -> None:
        with (
            mock.patch("subprocess.run", side_effect=_tools({"v1": _info("v1")})),
            mock.patch("arc.ingest.youtube._download_subtitle") as dl,
            mock.patch("arc.ingest.youtube.resolve_ffmpeg", return_value=FFMPEG),
        ):
            fetch_youtube(
                db,
                settings,
                channels=CHANNELS,
                force_audio=True,
                transcriber=FixtureTranscriber(text="x"),
                now=NOW,
            )
        dl.assert_not_called()
        assert IngestCursorRepo(db).get(BACKOFF_KEY) is None


# ---------------------------------------------------------------------------
# Backoff math
# ---------------------------------------------------------------------------


class TestBackoffMath:
    def test_formula_doubles_and_caps(self) -> None:
        s = _settings(yt_caption_cooldown_jitter=0)  # defaults 30 / 360
        rng = random.Random(0)
        got = [cooldown_minutes(n, s, rng) for n in range(1, 7)]
        assert got == [30, 60, 120, 240, 360, 360]

    def test_retry_after_wins_when_larger(self) -> None:
        s = _settings(yt_caption_cooldown_jitter=0)
        assert cooldown_minutes(1, s, random.Random(0), retry_after_s=7200) == 120
        assert cooldown_minutes(1, s, random.Random(0), retry_after_s=60) == 30

    def test_retry_after_flows_into_persisted_until(self, db, settings) -> None:
        s = _settings(yt_caption_cooldown_jitter=0)
        rl = CaptionResult(CaptionStatus.RATE_LIMITED, http_status=429, retry_after_s=5 * 3600)
        *_, logs = _fetch(db, s, {"v1": _info("v1")}, [rl])
        state = load_backoff(db)
        assert state.cooldown_until == NOW + timedelta(hours=5)
        (ev,) = _events(logs, "youtube.captions_rate_limited")
        assert ev["retry_after"] is True

    def test_jitter_deterministic_with_seeded_rng(self) -> None:
        s = _settings()
        a = [cooldown_minutes(n, s, random.Random(42)) for n in range(1, 5)]
        b = [cooldown_minutes(n, s, random.Random(42)) for n in range(1, 5)]
        assert a == b
        assert cooldown_minutes(1, s, random.Random(1)) != cooldown_minutes(1, s, random.Random(2))

    @given(
        n=st.integers(min_value=1, max_value=40),
        seed=st.integers(min_value=0, max_value=2**32),
        base=st.floats(min_value=1, max_value=120),
        cap=st.floats(min_value=1, max_value=1000),
        jitter=st.floats(min_value=0, max_value=0.5),
    )
    def test_jitter_bounds(self, n, seed, base, cap, jitter) -> None:
        s = _settings(
            yt_caption_cooldown_base_minutes=base,
            yt_caption_cooldown_max_minutes=cap,
            yt_caption_cooldown_jitter=jitter,
        )
        nominal = min(base * 2 ** (n - 1), cap)
        got = cooldown_minutes(n, s, random.Random(seed))
        assert nominal * (1 - jitter) - 1e-9 <= got <= nominal * (1 + jitter) + 1e-9

    def test_register_rate_limit_increments_and_stores_et(self) -> None:
        s = _settings(yt_caption_cooldown_jitter=0)
        state, minutes = register_rate_limit(
            CaptionBackoff(consecutive_rate_limits=1), s, random.Random(0), now=NOW
        )
        assert state.consecutive_rate_limits == 2 and minutes == 60
        assert state.cooldown_until == NOW + timedelta(minutes=60)
        assert state.cooldown_until is not None
        assert str(state.cooldown_until.tzinfo) == "America/New_York"

    def test_backoff_model_forbids_extra(self) -> None:
        with pytest.raises(ValueError):
            CaptionBackoff.model_validate({"consecutive_rate_limits": 1, "bogus": 1})


# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------


class TestSettings:
    def test_defaults(self) -> None:
        s = ArcSettings(env="paper")
        assert s.yt_caption_sleep_seconds == 5
        assert s.yt_caption_cooldown_base_minutes == 30
        assert s.yt_caption_cooldown_max_minutes == 360
        assert s.yt_caption_cooldown_jitter == pytest.approx(0.10)

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARC_YT_CAPTION_SLEEP_SECONDS", "9")
        monkeypatch.setenv("ARC_YT_CAPTION_COOLDOWN_BASE_MINUTES", "15")
        monkeypatch.setenv("ARC_YT_CAPTION_COOLDOWN_MAX_MINUTES", "90")
        monkeypatch.setenv("ARC_YT_CAPTION_COOLDOWN_JITTER", "0")
        s = ArcSettings(env="paper")
        assert s.yt_caption_sleep_seconds == 9
        assert s.yt_caption_cooldown_base_minutes == 15
        assert s.yt_caption_cooldown_max_minutes == 90
        assert [cooldown_minutes(n, s, random.Random(0)) for n in (1, 2, 3, 4)] == [
            15,
            30,
            60,
            90,
        ]
