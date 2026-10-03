"""Descriptor accounting for ``levi live doctor``."""

import os

from levi.live import cli, resources


def test_fd_count_and_limit_read_the_process_itself():
    pid = os.getpid()
    before = resources.fd_count(pid)
    with open(__file__):
        assert resources.fd_count(pid) == before + 1
    assert resources.fd_soft_limit(pid) > 0
    assert resources.fd_count(2**22 + 1) is None  # no such process
    assert resources.fd_soft_limit(2**22 + 1) is None


def test_doctor_warns_above_half_of_the_descriptor_limit():
    row = {"pid": 7, "fds": 513, "fd_limit": 1024}
    assert "513 open files" in cli.fd_warning(row)
    assert cli.fd_warning({**row, "fds": 512}) is None
    assert cli.fd_warning({**row, "fd_limit": None}) is None  # unlimited or unknown
    assert cli.fd_warning({**row, "fds": None}) is None
