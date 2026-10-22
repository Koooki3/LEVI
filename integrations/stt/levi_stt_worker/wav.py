"""Strict WAV validation with the standard library only.

Exactly the 44-byte header the browser writes (design record 5.5): no extra
chunks, no other sample format, no decoder library is ever pointed at the
untrusted bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .common import MIN_SECONDS, SAMPLE_RATE, TAIL_ALLOWANCE_S, WAV_HEADER_BYTES
from .errors import RequestError

_BYTES_PER_SECOND = SAMPLE_RATE * 2
_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")


@dataclass(frozen=True)
class WavClip:
    """Mono 16 kHz signed 16-bit little-endian samples."""

    pcm: bytes

    @property
    def seconds(self) -> float:
        return len(self.pcm) / _BYTES_PER_SECOND


def parse_wav(data: bytes, max_seconds: float) -> WavClip:
    if len(data) < WAV_HEADER_BYTES:
        raise RequestError("audio_invalid", "The audio is not a complete WAV file.")
    (
        riff,
        riff_size,
        wave,
        fmt,
        fmt_size,
        tag,
        channels,
        rate,
        byte_rate,
        block,
        bits,
        data_tag,
        data_size,
    ) = _HEADER.unpack_from(data)
    if (riff, wave, fmt, data_tag) != (b"RIFF", b"WAVE", b"fmt ", b"data"):
        raise RequestError("audio_invalid", "The audio is not a canonical WAV file.")
    if (fmt_size, tag, channels, rate, bits) != (16, 1, 1, SAMPLE_RATE, 16):
        raise RequestError("audio_invalid", "The audio must be 16 kHz mono 16-bit PCM.")
    if byte_rate != _BYTES_PER_SECOND or block != 2:
        raise RequestError("audio_invalid", "The WAV header is inconsistent.")
    if (
        riff_size != len(data) - 8
        or data_size != len(data) - WAV_HEADER_BYTES
        or data_size % 2
    ):
        raise RequestError(
            "audio_invalid", "The WAV chunk sizes do not match the data."
        )
    clip = WavClip(bytes(data[WAV_HEADER_BYTES:]))
    if clip.seconds < MIN_SECONDS:
        raise RequestError("audio_invalid", "The audio is shorter than 0.3 s.")
    if clip.seconds > max_seconds + TAIL_ALLOWANCE_S:
        raise RequestError(
            "too_long", "The audio is longer than the allowed recording time."
        )
    return clip
