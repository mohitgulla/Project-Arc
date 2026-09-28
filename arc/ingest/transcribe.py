"""Local audio transcription for YouTube videos without captions (E4.1b, D15).

Pipeline: ``yt-dlp -f bestaudio -x --audio-format m4a`` into a private temp
dir, then ``ffmpeg`` to 16 kHz mono PCM WAV, then a :class:`Transcriber`.
The temp dir (and every audio file in it) is deleted when transcription
finishes, even if a step fails.

The default backend is local ``mlx-whisper`` (Apple Silicon, no API key, no
cost). There is deliberately no cloud STT backend (D3: no new paid providers).
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
import structlog

log = structlog.get_logger()

SAMPLE_RATE = 16_000
DEFAULT_WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
_HERMES_FFMPEG_GLOB = str(Path.home() / ".hermes" / "tools" / "ffmpeg-*" / "ffmpeg")


class TranscriptionError(RuntimeError):
    """Audio could not be downloaded, converted or transcribed."""


# ---------------------------------------------------------------------------
# Transcribers
# ---------------------------------------------------------------------------


@runtime_checkable
class Transcriber(Protocol):
    """Turns a 16 kHz mono WAV file into plain text."""

    name: str

    def transcribe(self, wav_path: Path) -> str: ...


def load_wav_16k_mono(wav_path: Path) -> np.ndarray:
    """Read a 16-bit PCM mono 16 kHz WAV as float32 in [-1, 1]."""
    with wave.open(str(wav_path), "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != SAMPLE_RATE or w.getsampwidth() != 2:
            msg = (
                f"expected 16-bit mono {SAMPLE_RATE} Hz WAV, got {w.getsampwidth() * 8}-bit "
                f"{w.getnchannels()}ch {w.getframerate()} Hz"
            )
            raise TranscriptionError(msg)
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


@dataclass
class MlxWhisperTranscriber:
    """Local Whisper on Apple Silicon via ``mlx-whisper``.

    The model is fetched from Hugging Face on first use and cached there.
    """

    model: str = DEFAULT_WHISPER_MODEL
    name: str = "mlx-whisper"

    def transcribe(self, wav_path: Path) -> str:
        try:
            import mlx_whisper
        except ImportError as exc:  # non-Apple-Silicon hosts (e.g. Linux CI)
            msg = "mlx-whisper is not installed (Apple Silicon only)"
            raise TranscriptionError(msg) from exc

        audio = load_wav_16k_mono(wav_path)
        result = mlx_whisper.transcribe(audio, path_or_hf_repo=self.model, language="en")
        return " ".join(str(result.get("text", "")).split())


@dataclass
class FixtureTranscriber:
    """Deterministic transcriber for tests: returns canned text, records calls."""

    text: str = "fixture transcript"
    name: str = "fixture"
    calls: list[Path] = field(default_factory=list)

    def transcribe(self, wav_path: Path) -> str:
        if not wav_path.exists():
            msg = f"missing wav: {wav_path}"
            raise TranscriptionError(msg)
        self.calls.append(wav_path)
        return self.text


# ---------------------------------------------------------------------------
# ffmpeg / yt-dlp
# ---------------------------------------------------------------------------


def resolve_ffmpeg(explicit: str = "") -> str:
    """Locate ffmpeg: ``ARC_FFMPEG_BIN`` (explicit) → PATH → ``~/.hermes/tools/ffmpeg-*``."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(on_path)
    candidates.extend(sorted(glob.glob(_HERMES_FFMPEG_GLOB), reverse=True))
    for c in candidates:
        if Path(c).is_file() and os.access(c, os.X_OK):
            return c
    msg = "ffmpeg not found (set ARC_FFMPEG_BIN)"
    raise TranscriptionError(msg)


def _run(cmd: list[str], *, timeout: int, step: str) -> None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        msg = f"{step} failed: {exc}"
        raise TranscriptionError(msg) from exc
    if result.returncode != 0:
        msg = f"{step} failed (exit {result.returncode}): {(result.stderr or '')[-300:]}"
        raise TranscriptionError(msg)


def download_audio(video_url: str, workdir: Path, *, ffmpeg: str) -> Path:
    """Download the best audio stream as m4a into ``workdir``."""
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-f",
        "bestaudio",
        "-x",
        "--audio-format",
        "m4a",
        "--ffmpeg-location",
        ffmpeg,
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "-o",
        str(workdir / "audio.%(ext)s"),
        video_url,
    ]
    _run(cmd, timeout=900, step="yt-dlp audio download")
    out = workdir / "audio.m4a"
    if not out.exists():
        msg = "yt-dlp produced no audio.m4a"
        raise TranscriptionError(msg)
    return out


def to_wav_16k_mono(src: Path, dst: Path, *, ffmpeg: str) -> Path:
    """Convert any audio file to 16 kHz mono 16-bit PCM WAV."""
    cmd = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    _run(cmd, timeout=600, step="ffmpeg resample")
    if not dst.exists():
        msg = "ffmpeg produced no wav"
        raise TranscriptionError(msg)
    return dst


def transcribe_video_audio(
    video_url: str,
    transcriber: Transcriber,
    *,
    ffmpeg: str,
) -> str:
    """Download → 16 kHz mono → transcribe. Audio files are always deleted."""
    with tempfile.TemporaryDirectory(prefix="arc-yt-audio-") as tmp:
        workdir = Path(tmp)
        m4a = download_audio(video_url, workdir, ffmpeg=ffmpeg)
        wav = to_wav_16k_mono(m4a, workdir / "audio16k.wav", ffmpeg=ffmpeg)
        log.info("transcribe.start", url=video_url, backend=transcriber.name)
        text = transcriber.transcribe(wav)
    log.info("transcribe.done", url=video_url, backend=transcriber.name, chars=len(text))
    return text
