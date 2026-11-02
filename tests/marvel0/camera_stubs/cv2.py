"""A stand-in for OpenCV for the tests that run the real camera preview worker
(see ``pyrealsense2.py`` in this folder). ``STUB_CV2_FAIL=1`` makes every
picture fail to encode, which ends the worker with exit code 13 at once."""

import os

IMWRITE_JPEG_QUALITY = 1


class _Buffer:
    def __init__(self, data):
        self._data = data

    def tobytes(self):
        return self._data


def setNumThreads(count):
    pass


def imencode(extension, image, params):
    if os.environ.get("STUB_CV2_FAIL") == "1":
        return False, None
    return True, _Buffer(b"\xff\xd8\xff" + bytes(image) + b"\xff\xd9")
