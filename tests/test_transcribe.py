"""Tests for the YouTube audio-transcription fallback (E4.1b, D15).

No network: yt-dlp / ffmpeg subprocesses are faked, and the transcriber is a
``FixtureTranscriber``.
"""

from __future__ import annotations

import json
import sqlite3
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from arc.config import ArcSettings
from arc.ingest.caption_backoff import CaptionResult, CaptionStatus
from arc.ingest.transcribe import (
    FixtureTranscriber,
    MlxWhisperTranscriber,
    Transcriber,
    TranscriptionError,
    load_wav_16k_mono,
    resolve_ffmpeg,
    transcribe_video_audio,
)
from arc.ingest.youtube import fetch_youtube, transcript_source_of
from arc.models import TranscriptSource
from arc.store.migrate import migrate

NOW = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)  # 22:00 ET Scalp run
FFMPEG = "/fake/ffmpeg"


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


@pytest.fixture()
def settings() -> ArcSettings:
    return ArcSettings(
        env="paper",
        ingest_youtube_channels=["https://www.youtube.com/@TestChannel"],
        universe=["SPY", "NVDA"],
        ffmpeg_bin=FFMPEG,
    )


def _write_wav(path: Path, *, rate: int = 16_000, channels: int = 1, n: int = 1600) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * n * channels)


def _info(
    vid: str,
    *,
    age_min: float = 120,
    duration: float | None = 900,
    captions: bool = False,
    manual: bool = False,
) -> dict:
    auto = {"en": [{"ext": "vtt", "url": f"https://captions.test/{vid}.vtt"}]} if captions else {}
    subs = {"en": [{"ext": "vtt", "url": f"https://manual.test/{vid}.vtt"}]} if manual else {}
    info: dict = {
        "id": vid,
        "title": f"Title {vid}",
        "channel": "StockedUp",
        "upload_date": (NOW - timedelta(minutes=age_min)).strftime("%Y%m%d"),
        "timestamp": int((NOW - timedelta(minutes=age_min)).timestamp()),
        "subtitles": subs,
        "automatic_captions": auto,
    }
    if duration is not None:
        info["duration"] = duration
    return info


class FakeTools:
    """Fake ``subprocess.run`` for yt-dlp (list, info, audio) and ffmpeg."""

    def __init__(self, infos: dict[str, dict], *, fail_ffmpeg: bool = False) -> None:
        self.infos = infos
        self.fail_ffmpeg = fail_ffmpeg
        self.audio_dirs: list[Path] = []
        self.downloads: list[str] = []

    def __call__(self, cmd: list[str], **_kw: object) -> mock.MagicMock:
        r = mock.MagicMock(returncode=0, stdout="", stderr="")
        if "--flat-playlist" in cmd:
            r.stdout = "\n".join(json.dumps({"id": v}) for v in self.infos)
        elif "--dump-single-json" in cmd:
            r.stdout = json.dumps(self.infos[cmd[-1].rsplit("=", 1)[-1]])
        elif "bestaudio" in cmd:
            out = Path(cmd[cmd.index("-o") + 1].replace("%(ext)s", "m4a"))
            out.write_bytes(b"fake-m4a")
            self.audio_dirs.append(out.parent)
            self.downloads.append(cmd[-1])
        elif cmd[0] == FFMPEG:
            if self.fail_ffmpeg:
                r.returncode = 1
                r.stderr = "boom"
            else:
                _write_wav(Path(cmd[-1]))
        else:  # pragma: no cover - unexpected command
            r.returncode = 1
        return r


def _run(db, settings, infos, *, force=False, tx=None, fail_ffmpeg=False, subs="caption text"):
    tools = FakeTools(infos, fail_ffmpeg=fail_ffmpeg)
    tx = tx or FixtureTranscriber(text="spy is holding 580 support")
    with (
        mock.patch("subprocess.run", side_effect=tools),
        mock.patch(
            "arc.ingest.youtube._download_subtitle",
            return_value=CaptionResult.ok(subs)
            if subs
            else CaptionResult(CaptionStatus.EMPTY, http_status=200),
        ) as dl,
        mock.patch("arc.ingest.youtube.resolve_ffmpeg", return_value=FFMPEG),
    ):
        docs = fetch_youtube(
            db, settings, force_audio=force, transcriber=tx, now=NOW, sleep=lambda _s: None
        )
    return docs, tools, tx, dl


# ---------------------------------------------------------------------------
# Fallback order
# ---------------------------------------------------------------------------


class TestFallbackOrder:
    def test_manual_subs_first(self, db, settings) -> None:
        docs, tools, tx, dl = _run(db, settings, {"v1": _info("v1", captions=True, manual=True)})
        dl.assert_called_once_with("https://manual.test/v1.vtt")
        assert tx.calls == [] and tools.downloads == []
        assert docs[0].transcript_source is TranscriptSource.CAPTIONS
        assert docs[0].text.startswith("[transcript:captions] [StockedUp] [Title v1] caption")

    def test_auto_captions_second(self, db, settings) -> None:
        docs, tools, tx, dl = _run(db, settings, {"v1": _info("v1", captions=True)})
        dl.assert_called_once_with("https://captions.test/v1.vtt")
        assert tx.calls == []
        assert docs[0].transcript_source is TranscriptSource.CAPTIONS

    def test_audio_when_no_captions(self, db, settings) -> None:
        docs, tools, tx, dl = _run(db, settings, {"v1": _info("v1")})
        dl.assert_not_called()
        assert tools.downloads == ["https://www.youtube.com/watch?v=v1"]
        assert len(tx.calls) == 1
        assert docs[0].transcript_source is TranscriptSource.AUDIO
        assert docs[0].text == (
            "[transcript:audio] [StockedUp] [Title v1] spy is holding 580 support"
        )
        assert docs[0].tickers_hint == ["SPY"]
        row = db.execute("SELECT text FROM raw_docs").fetchone()
        assert transcript_source_of(row["text"]) is TranscriptSource.AUDIO

    def test_audio_when_caption_download_empty(self, db, settings) -> None:
        docs, _, tx, dl = _run(db, settings, {"v1": _info("v1", captions=True)}, subs="")
        dl.assert_called_once()
        assert len(tx.calls) == 1
        assert docs[0].transcript_source is TranscriptSource.AUDIO

    def test_force_audio_skips_captions(self, db, settings) -> None:
        docs, _, tx, dl = _run(db, settings, {"v1": _info("v1", captions=True)}, force=True)
        dl.assert_not_called()
        assert len(tx.calls) == 1
        assert docs[0].transcript_source is TranscriptSource.AUDIO

    def test_transcription_failure_is_retried_next_run(self, db, settings) -> None:
        docs, _, tx, _ = _run(db, settings, {"v1": _info("v1")}, fail_ffmpeg=True)
        assert docs == [] and tx.calls == []
        docs, _, _, _ = _run(db, settings, {"v1": _info("v1")})
        assert [d.url for d in docs] == ["https://www.youtube.com/watch?v=v1"]


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


class TestGuardRails:
    def test_grace_period_waits_for_captions(self, db, settings) -> None:
        docs, tools, tx, _ = _run(db, settings, {"v1": _info("v1", age_min=10)})
        assert docs == [] and tx.calls == [] and tools.downloads == []

    def test_grace_period_elapsed(self, db, settings) -> None:
        docs, _, _, _ = _run(db, settings, {"v1": _info("v1", age_min=31)})
        assert len(docs) == 1

    def test_grace_period_configurable(self, db) -> None:
        s = ArcSettings(
            env="paper",
            ingest_youtube_channels=["https://www.youtube.com/@T"],
            ffmpeg_bin=FFMPEG,
            yt_caption_grace_minutes=0,
        )
        docs, _, _, _ = _run(db, s, {"v1": _info("v1", age_min=1)})
        assert len(docs) == 1

    def test_unknown_upload_time_waits(self, db, settings) -> None:
        info = _info("v1")
        del info["timestamp"]
        docs, _, tx, _ = _run(db, settings, {"v1": info})
        assert docs == [] and tx.calls == []

    def test_force_audio_ignores_grace(self, db, settings) -> None:
        docs, _, _, _ = _run(db, settings, {"v1": _info("v1", age_min=1)}, force=True)
        assert len(docs) == 1

    def test_max_duration_skip(self, db, settings) -> None:
        with mock.patch("arc.ingest.youtube.log") as log:
            docs, tools, tx, _ = _run(
                db, settings, {"v1": _info("v1", duration=61 * 60)}, force=True
            )
        assert docs == [] and tx.calls == [] and tools.downloads == []
        log.info.assert_any_call(
            "youtube.audio_skipped",
            url="https://www.youtube.com/watch?v=v1",
            reason="too_long",
            duration_s=61 * 60,
            max_minutes=60,
        )

    def test_exactly_max_duration_allowed(self, db, settings) -> None:
        docs, _, _, _ = _run(db, settings, {"v1": _info("v1", duration=60 * 60)})
        assert len(docs) == 1

    def test_unknown_duration_skipped(self, db, settings) -> None:
        docs, _, tx, _ = _run(db, settings, {"v1": _info("v1", duration=None)}, force=True)
        assert docs == [] and tx.calls == []

    def test_live_stream_skipped(self, db, settings) -> None:
        info = _info("v1") | {"live_status": "is_live"}
        docs, _, tx, _ = _run(db, settings, {"v1": info}, force=True)
        assert docs == [] and tx.calls == []

    def test_at_most_three_per_run(self, db, settings) -> None:
        infos = {f"v{i}": _info(f"v{i}") for i in range(5)}
        docs, tools, tx, _ = _run(db, settings, infos)
        assert len(tx.calls) == 3 and len(docs) == 3
        # the remaining two are left for the next run
        docs2, _, tx2, _ = _run(db, settings, infos)
        assert len(docs2) == 2 and len(tx2.calls) == 2

    def test_failed_attempt_counts_toward_cap(self, db, settings) -> None:
        infos = {f"v{i}": _info(f"v{i}") for i in range(5)}
        _, tools, _, _ = _run(db, settings, infos, fail_ffmpeg=True)
        assert len(tools.downloads) == 3

    def test_captioned_videos_do_not_use_cap(self, db, settings) -> None:
        infos = {f"c{i}": _info(f"c{i}", captions=True) for i in range(4)}
        infos |= {f"a{i}": _info(f"a{i}") for i in range(3)}
        docs, _, tx, _ = _run(db, settings, infos)
        assert len(docs) == 7 and len(tx.calls) == 3


# ---------------------------------------------------------------------------
# Audio pipeline + cleanup
# ---------------------------------------------------------------------------


class TestAudioPipeline:
    def test_temp_files_cleaned_up(self, db, settings) -> None:
        docs, tools, _, _ = _run(db, settings, {"v1": _info("v1")})
        assert len(docs) == 1
        assert tools.audio_dirs and all(not d.exists() for d in tools.audio_dirs)

    def test_temp_files_cleaned_up_on_failure(self, db, settings) -> None:
        _, tools, _, _ = _run(db, settings, {"v1": _info("v1")}, fail_ffmpeg=True)
        assert tools.audio_dirs and all(not d.exists() for d in tools.audio_dirs)

    def test_temp_files_cleaned_up_when_transcriber_raises(self) -> None:
        class Boom:
            name = "boom"

            def transcribe(self, wav_path: Path) -> str:
                raise TranscriptionError("model crashed")

        tools = FakeTools({})
        with (
            mock.patch("subprocess.run", side_effect=tools),
            pytest.raises(TranscriptionError),
        ):
            transcribe_video_audio("https://y/watch?v=x", Boom(), ffmpeg=FFMPEG)
        assert tools.audio_dirs and not tools.audio_dirs[0].exists()

    def test_pipeline_commands(self) -> None:
        tools = FakeTools({})
        calls: list[list[str]] = []

        def spy(cmd, **kw):
            calls.append(cmd)
            return tools(cmd, **kw)

        tx = FixtureTranscriber(text="hello")
        with mock.patch("subprocess.run", side_effect=spy):
            assert transcribe_video_audio("https://y/watch?v=x", tx, ffmpeg=FFMPEG) == "hello"
        ytdlp, ff = calls
        assert ytdlp[ytdlp.index("-f") + 1] == "bestaudio"
        assert "-x" in ytdlp and ytdlp[ytdlp.index("--audio-format") + 1] == "m4a"
        assert ytdlp[ytdlp.index("--ffmpeg-location") + 1] == FFMPEG
        assert ff[0] == FFMPEG
        assert ff[ff.index("-ar") + 1] == "16000" and ff[ff.index("-ac") + 1] == "1"
        assert tx.calls[0].name == "audio16k.wav"

    def test_download_timeout_raises(self) -> None:
        import subprocess

        with (
            mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("yt", 1)),
            pytest.raises(TranscriptionError, match="yt-dlp"),
        ):
            transcribe_video_audio("u", FixtureTranscriber(), ffmpeg=FFMPEG)

    def test_download_without_output_raises(self) -> None:
        ok = mock.MagicMock(returncode=0, stdout="", stderr="")
        with (
            mock.patch("subprocess.run", return_value=ok),
            pytest.raises(TranscriptionError, match="no audio"),
        ):
            transcribe_video_audio("u", FixtureTranscriber(), ffmpeg=FFMPEG)


class TestTranscribers:
    def test_fixture_is_a_transcriber(self, tmp_path: Path) -> None:
        tx = FixtureTranscriber(text="abc")
        assert isinstance(tx, Transcriber)
        wav = tmp_path / "a.wav"
        _write_wav(wav)
        assert tx.transcribe(wav) == "abc" and tx.calls == [wav]
        with pytest.raises(TranscriptionError):
            tx.transcribe(tmp_path / "missing.wav")

    def test_mlx_backend_defaults(self) -> None:
        tx = MlxWhisperTranscriber()
        assert isinstance(tx, Transcriber)
        assert tx.model == "mlx-community/whisper-large-v3-turbo"
        assert ArcSettings(env="paper").whisper_model == tx.model

    def test_mlx_backend_calls_library(self, tmp_path: Path) -> None:
        wav = tmp_path / "a.wav"
        _write_wav(wav)
        fake = mock.MagicMock()
        fake.transcribe.return_value = {"text": "  SPY   up\n today "}
        with mock.patch.dict("sys.modules", {"mlx_whisper": fake}):
            out = MlxWhisperTranscriber(model="m").transcribe(wav)
        assert out == "SPY up today"
        audio = fake.transcribe.call_args.args[0]
        assert audio.dtype.name == "float32" and audio.shape == (1600,)
        assert fake.transcribe.call_args.kwargs == {"path_or_hf_repo": "m", "language": "en"}

    def test_mlx_backend_missing_library(self, tmp_path: Path) -> None:
        wav = tmp_path / "a.wav"
        _write_wav(wav)
        with (
            mock.patch.dict("sys.modules", {"mlx_whisper": None}),
            pytest.raises(TranscriptionError, match="Apple Silicon"),
        ):
            MlxWhisperTranscriber().transcribe(wav)

    def test_wav_format_enforced(self, tmp_path: Path) -> None:
        wav = tmp_path / "stereo.wav"
        _write_wav(wav, rate=44_100, channels=2)
        with pytest.raises(TranscriptionError, match="16-bit mono"):
            load_wav_16k_mono(wav)


class TestResolveFfmpeg:
    def test_explicit_wins(self, tmp_path: Path) -> None:
        exe = tmp_path / "ffmpeg"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        assert resolve_ffmpeg(str(exe)) == str(exe)

    def test_path_then_hermes_tools(self, tmp_path: Path) -> None:
        exe = tmp_path / "ffmpeg"
        exe.write_text("")
        exe.chmod(0o755)
        with (
            mock.patch("shutil.which", return_value=None),
            mock.patch("glob.glob", return_value=[str(exe)]),
        ):
            assert resolve_ffmpeg("") == str(exe)

    def test_missing_raises(self) -> None:
        with (
            mock.patch("shutil.which", return_value=None),
            mock.patch("glob.glob", return_value=[]),
            pytest.raises(TranscriptionError, match="ARC_FFMPEG_BIN"),
        ):
            resolve_ffmpeg("/nope/ffmpeg")


class TestTranscriptSourceOf:
    def test_prefixes(self) -> None:
        assert transcript_source_of("[transcript:audio] x") is TranscriptSource.AUDIO
        assert transcript_source_of("[transcript:captions] x") is TranscriptSource.CAPTIONS
        assert transcript_source_of("[StockedUp] legacy") is None


class TestCli:
    def test_ingest_force_audio_flag(self, tmp_path: Path) -> None:
        from arc.cli import main

        with mock.patch("arc.ingest.youtube.fetch_youtube", return_value=[]) as fy:
            assert main(["ingest", "youtube", "--force-audio", "--db", str(tmp_path / "a.db")]) == 0
        assert fy.call_args.kwargs == {"force_audio": True, "max_videos": 5}
