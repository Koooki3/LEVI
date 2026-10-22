"""The one audio format the page may upload, checked byte by byte.

16 kHz, mono, 16-bit PCM in a WAV file whose header is exactly 44 bytes: the
``RIFF`` header, a 16-byte ``fmt `` chunk, and a ``data`` chunk that runs to
the end of the file. Anything else (another rate, stereo, an extra chunk, a
size that does not match the bytes) is refused. No decoder, no ffmpeg and no
library sees untrusted bytes here; the worker reads the samples itself, and
only after this check.
"""

import struct
from dataclasses import dataclass

from .settings import MAX_BYTES

HEADER_BYTES = 44
SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2
MIN_SECONDS = 0.3
MAX_SECONDS = 10.5
CONTENT_TYPE = "audio/wav"

# (RIFF, riff size, WAVE, "fmt ", fmt size, format, channels, rate, byte rate,
# block align, bits, "data", data size)
_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")


class WavError(ValueError):
    """``code`` is ``format``, ``too_short``, ``too_long`` or ``too_large``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Wav:
    seconds: float
    data_bytes: int


def parse(data: bytes) -> Wav:
    """The accepted file's length, or the reason it is refused."""
    if len(data) > MAX_BYTES:
        raise WavError("too_large", "the audio is larger than the limit")
    if len(data) < HEADER_BYTES:
        raise WavError("format", "the audio is shorter than a WAV header")
    (
        riff,
        riff_size,
        wave,
        fmt,
        fmt_size,
        audio_format,
        channels,
        rate,
        byte_rate,
        block_align,
        bits,
        data_id,
        data_size,
    ) = _HEADER.unpack_from(data)
    if (riff, wave, fmt, data_id) != (b"RIFF", b"WAVE", b"fmt ", b"data"):
        raise WavError("format", "not a WAV file with a 44-byte header")
    if (fmt_size, audio_format, channels, bits) != (16, 1, 1, 16):
        raise WavError("format", "the audio is not 16-bit mono PCM")
    if rate != SAMPLE_RATE or byte_rate != BYTES_PER_SECOND or block_align != 2:
        raise WavError("format", "the audio is not sampled at 16 kHz")
    if riff_size != len(data) - 8 or data_size != len(data) - HEADER_BYTES:
        raise WavError("format", "the sizes in the header do not match the bytes")
    if data_size % 2:
        raise WavError("format", "the sample data is not whole samples")
    seconds = data_size / BYTES_PER_SECOND
    if seconds < MIN_SECONDS:
        raise WavError("too_short", f"the audio is shorter than {MIN_SECONDS} s")
    if seconds > MAX_SECONDS:
        raise WavError("too_long", f"the audio is longer than {MAX_SECONDS} s")
    return Wav(seconds=seconds, data_bytes=data_size)
