"""Audio input/output helpers for the realtime CLI.

PyAudio is an optional dependency. If it is not installed, the CLI falls back to
text-only mode. The `pyaudio` package requires PortAudio system libraries.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import cast

logger = logging.getLogger(__name__)


try:
    import pyaudio

    _HAS_PYAUDIO = True
except ImportError:
    pyaudio = None
    _HAS_PYAUDIO = False


class AudioIOError(Exception):
    """Audio hardware or PyAudio configuration problem."""


class AudioRecorder:
    """Blocking microphone recorder. Run in a thread for async use."""

    def __init__(
        self,
        sample_rate: int = 24000,
        channels: int = 1,
        chunk_ms: int = 200,
        format_name: str = "pcm_s16le",
    ) -> None:
        if not _HAS_PYAUDIO:
            raise AudioIOError("PyAudio is not installed. Install 'monkeybot[cli-realtime]'.")
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_ms = chunk_ms
        self.format_name = format_name
        self._format = pyaudio.paInt16 if "s16" in format_name else pyaudio.paInt8
        self._bytes_per_sample = 2 if "s16" in format_name else 1
        self._chunk_frames = int(sample_rate * chunk_ms / 1000)
        self._chunk_bytes = self._chunk_frames * self._bytes_per_sample * channels
        try:
            self._pyaudio = pyaudio.PyAudio()
            self._stream = self._pyaudio.open(
                format=self._format,
                channels=channels,
                rate=sample_rate,
                input=True,
                frames_per_buffer=self._chunk_frames,
            )
        except Exception as exc:
            raise AudioIOError(f"Failed to open microphone: {exc}") from exc
        logger.info(
            "audio recorder opened: rate=%s channels=%s chunk_ms=%s",
            sample_rate,
            channels,
            chunk_ms,
        )

    def read_chunk(self) -> bytes:
        try:
            return cast(bytes, self._stream.read(self._chunk_frames, exception_on_overflow=False))
        except Exception as exc:
            raise AudioIOError(f"Microphone read failed: {exc}") from exc

    def chunk_peak_db(self, chunk: bytes) -> float:
        """Return approximate peak level of a PCM s16le chunk in dBFS."""
        import math
        import struct

        if not chunk or self._bytes_per_sample != 2:
            return -96.0
        count = len(chunk) // 2
        if count == 0:
            return -96.0
        peak = 0
        # Unpack every 16-bit sample and find max absolute value.
        for i in range(0, len(chunk) - 1, 2):
            sample = struct.unpack("<h", chunk[i : i + 2])[0]
            abs_sample = abs(sample)
            if abs_sample > peak:
                peak = abs_sample
        if peak == 0:
            return -96.0
        return 20.0 * math.log10(peak / 32768.0)

    def iter_chunks(self) -> Iterator[bytes]:
        while True:
            yield self.read_chunk()

    def close(self) -> None:
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            logger.exception("audio recorder close failed")
        finally:
            self._pyaudio.terminate()


class AudioPlayer:
    """Blocking speaker output. Run in a thread for async use."""

    def __init__(
        self,
        sample_rate: int = 24000,
        channels: int = 1,
        format_name: str = "pcm_s16le",
    ) -> None:
        if not _HAS_PYAUDIO:
            raise AudioIOError("PyAudio is not installed. Install 'monkeybot[cli-realtime]'.")
        self.sample_rate = sample_rate
        self.channels = channels
        self._format = pyaudio.paInt16 if "s16" in format_name else pyaudio.paInt8
        try:
            self._pyaudio = pyaudio.PyAudio()
            self._stream = self._pyaudio.open(
                format=self._format,
                channels=channels,
                rate=sample_rate,
                output=True,
            )
        except Exception as exc:
            raise AudioIOError(f"Failed to open speaker: {exc}") from exc
        logger.info("audio player opened: rate=%s channels=%s", sample_rate, channels)

    def write(self, chunk: bytes) -> None:
        try:
            self._stream.write(chunk)
        except Exception as exc:
            raise AudioIOError(f"Speaker write failed: {exc}") from exc

    def close(self) -> None:
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            logger.exception("audio player close failed")
        finally:
            self._pyaudio.terminate()
