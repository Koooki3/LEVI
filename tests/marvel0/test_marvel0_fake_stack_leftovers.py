"""The leftover check of the stand-in rehearsals finds the stand-ins of its own
test and only those: under ``pytest -n`` another test file's stand-ins run at
the same time, and a whole-machine check blamed the wrong test."""

import subprocess
import sys
import time

from fake_stack import fake_specs


def _sleeper(*names):
    # A process whose command line looks like a stand-in's: it names a script
    # of fake_stack/ and a file under some test's folder.
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", *map(str, names)]
    )


def _wait_listed(root, present, limit_s=5.0):
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if bool(fake_specs.leftover_standins(root)) == present:
            return True
        time.sleep(0.05)
    return False


def test_a_stand_in_naming_the_tests_folder_is_found_and_others_are_not(tmp_path):
    mine, other, longer = tmp_path / "t1", tmp_path / "t2", tmp_path / "t10"
    for folder in (mine, other, longer):
        folder.mkdir()
    script = fake_specs.HERE / "fake_a.py"
    procs = [
        _sleeper(script, f"--health={other}/health.json"),
        _sleeper(script, f"--health={longer}/health.json"),
    ]
    try:
        # Another test's stand-ins, one in a folder whose name starts with ours.
        assert _wait_listed(other, present=True)
        assert fake_specs.leftover_standins(mine) == []
        procs.append(_sleeper(script, f"--health={mine}/health.json"))
        assert _wait_listed(mine, present=True)
        assert len(fake_specs.leftover_standins(mine)) == 1
    finally:
        for proc in procs:
            proc.kill()
            proc.wait(timeout=5)
    assert _wait_listed(mine, present=False)
