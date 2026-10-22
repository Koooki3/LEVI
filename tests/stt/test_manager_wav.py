"""``levi/stt/wav.py``: the one WAV layout the page may upload."""

import struct

import pytest

from levi.stt import wav
from levi.stt.settings import MAX_BYTES


def make(
    seconds=1.0,
    *,
    rate=16_000,
    channels=1,
    bits=16,
    fmt_size=16,
    audio_format=1,
    riff_delta=0,
    data_delta=0,
    byte_rate=None,
    block_align=None,
    magic=b"RIFF",
    wave=b"WAVE",
    fmt=b"fmt ",
    data_id=b"data",
    tail=b"",
    samples=None,
):
    count = samples if samples is not None else int(seconds * rate)
    pcm = b"\x01\x02" * count
    block = block_align if block_align is not None else channels * bits // 8
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        magic,
        36 + len(pcm) + len(tail) + riff_delta,
        wave,
        fmt,
        fmt_size,
        audio_format,
        channels,
        rate,
        byte_rate if byte_rate is not None else rate * block,
        block,
        bits,
        data_id,
        len(pcm) + data_delta,
    )
    return header + pcm + tail


def code(data):
    with pytest.raises(wav.WavError) as caught:
        wav.parse(data)
    return caught.value.code


def test_a_good_recording_is_accepted_with_its_length():
    parsed = wav.parse(make(2.5))
    assert parsed.seconds == pytest.approx(2.5) and parsed.data_bytes == 80_000


@pytest.mark.parametrize("seconds", [0.3, 1.0, 10.0, 10.5])
def test_the_limits_themselves_are_accepted(seconds):
    assert wav.parse(make(seconds)).seconds == pytest.approx(seconds)


def test_the_page_limit_of_ten_seconds_fits_in_the_byte_cap_with_room():
    assert wav.HEADER_BYTES + 10.5 * wav.BYTES_PER_SECOND < MAX_BYTES


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"RIFF",
        make(1.0)[:43],
        make(1.0, magic=b"RIFX"),
        make(1.0, wave=b"WAVX"),
        make(1.0, fmt=b"fmt\0"),
        make(1.0, fmt_size=18),
        make(1.0, audio_format=3),
        make(1.0, channels=2),
        make(1.0, rate=48_000),
        make(1.0, rate=8_000),
        make(1.0, bits=8),
        make(1.0, bits=24),
        make(1.0, byte_rate=31_999),
        make(1.0, block_align=4),
        make(1.0, data_id=b"LIST"),
        make(1.0, riff_delta=1),
        make(1.0, riff_delta=-1),
        make(1.0, data_delta=2),
        make(1.0, data_delta=-2),
        make(1.0, tail=b"LIST\x04\x00\x00\x00abcd"),
        make(1.0, tail=b"\x00"),
        b"not audio at all, but long enough to hold a header of forty-four bytes",
    ],
    ids=[
        "empty",
        "four-bytes",
        "short-header",
        "rifx",
        "wavx",
        "fmt-id",
        "fmt-size-18",
        "float-pcm",
        "stereo",
        "48k",
        "8k",
        "8-bit",
        "24-bit",
        "byte-rate",
        "block-align",
        "no-data-chunk-first",
        "riff-size-plus-one",
        "riff-size-minus-one",
        "data-size-too-big",
        "data-size-too-small",
        "extra-chunk-after-data",
        "trailing-byte",
        "text",
    ],
)
def test_anything_but_the_exact_layout_is_a_format_error(data):
    assert code(data) == "format"


def test_an_odd_number_of_data_bytes_is_not_whole_samples():
    data = bytearray(make(1.0)[:-1])
    struct.pack_into("<I", data, 4, len(data) - 8)  # both sizes agree with the bytes
    struct.pack_into("<I", data, 40, len(data) - 44)
    assert code(bytes(data)) == "format"


def test_a_recording_shorter_than_a_third_of_a_second_is_too_short():
    assert code(make(samples=4799)) == "too_short"
    assert code(make(samples=0)) == "too_short"


def test_a_recording_longer_than_ten_and_a_half_seconds_is_too_long():
    assert code(make(samples=168_001)) == "too_long"
    assert code(make(12.0)) == "too_long"


def test_a_file_over_the_byte_cap_is_too_large_before_anything_is_read():
    assert code(make(12.6)) == "too_large"
    assert code(b"\0" * (MAX_BYTES + 1)) == "too_large"
