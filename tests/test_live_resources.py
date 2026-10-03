"""Descriptor accounting for ``levi live doctor``."""

import contextlib
import os

from levi.live import cli, resources


def descriptors_on(path) -> int:
    target = os.path.realpath(path)
    count = 0
    for name in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):
            count += os.path.realpath(f"/proc/self/fd/{name}") == target
    return count


def test_fd_count_and_limit_read_the_process_itself():
    pid = os.getpid()
    handles = [open(__file__) for _ in range(3)]  # noqa: SIM115
    try:
        # Only descriptors on this file are counted exactly (other threads may
        # open or close theirs meanwhile); the total can only include them.
        assert descriptors_on(__file__) == 3
        assert resources.fd_count(pid) >= 3
    finally:
        for handle in handles:
            handle.close()
    assert descriptors_on(__file__) == 0
    assert resources.fd_soft_limit(pid) > 0
    assert resources.fd_count(2**22 + 1) is None  # no such process
    assert resources.fd_soft_limit(2**22 + 1) is None


def test_doctor_warns_above_half_of_the_descriptor_limit():
    row = {"pid": 7, "fds": 513, "fd_limit": 1024}
    assert "513 open files" in cli.fd_warning(row)
    assert cli.fd_warning({**row, "fds": 512}) is None
    assert cli.fd_warning({**row, "fd_limit": None}) is None  # unlimited or unknown
    assert cli.fd_warning({**row, "fds": None}) is None
