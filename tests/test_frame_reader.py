"""Evidence frames are read in order without a seek per frame, and are the
very frames a seek returns."""

import cv2
import numpy as np
import pytest

from levi.agent.media import SEEK_AHEAD, FrameReader


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "v.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    assert writer.isOpened()
    for i in range(SEEK_AHEAD * 3):
        frame = np.full((48, 64, 3), (i * 3) % 256, dtype=np.uint8)
        cv2.putText(frame, str(i), (2, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0))
        writer.write(frame)
    writer.release()
    return path


def sought(path, frame):
    cap = cv2.VideoCapture(str(path))
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
    ok, image = cap.read()
    cap.release()
    assert ok
    return image


def test_reading_on_returns_what_a_seek_returns(video):
    cap = cv2.VideoCapture(str(video))
    reader = FrameReader(cap)
    # In order and close (decoded on), a jump (sought) and a step back (sought).
    for frame in [0, 5, 6, 11, 11 + SEEK_AHEAD + 20, 3, 9]:
        assert np.array_equal(reader.read(frame), sought(video, frame)), frame
    cap.release()


def test_a_frame_past_the_end_fails(video):
    cap = cv2.VideoCapture(str(video))
    reader = FrameReader(cap)
    reader.read(SEEK_AHEAD * 3 - 1)
    with pytest.raises(ValueError, match="decoding/seek failed"):
        reader.read(SEEK_AHEAD * 3 + 1)
    cap.release()
