"""scripts/verify_conversion.py talks to the core, never through the web page.

The web bridge on :7860 refuses writes that do not come from the LEVI page
itself (docs/API.md, "Trust boundary of the web bridge"), so the script uses
the core's own channel with the human credential, like the `levi` CLI. No
service is started here: the core is replaced by a recorder.
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_conversion.py"


def load():
    spec = importlib.util.spec_from_file_location("verify_conversion", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # must not build fixtures or call anything
    return module


def test_the_script_does_not_address_the_web_page():
    source = SCRIPT.read_text()
    assert "7860" not in source
    assert "httpx" not in source
    assert "http://" not in source


def test_every_call_goes_to_the_core_as_the_person(tmp_path):
    calls = []

    def core(path, payload=None, *, human=False, **_):
        calls.append((path, payload, human))
        if path == "/api/levi/jobs/plan":
            return {"id": "j1"}
        if path == "/api/levi/jobs":
            return [{"id": "j1", "status": "succeeded", "dataset": "local/x"}]
        return {"ok": True}

    result = load().exercise(tmp_path / "raw", request=core, sleep=lambda _: None)

    assert [c[0] for c in calls] == [
        "/api/levi/jobs/plan",
        "/api/levi/jobs/j1/run",
        "/api/levi/jobs",
        "/api/levi/diagnostics",
    ]
    assert all(human for _, _, human in calls)
    assert calls[0][1] == {
        "stage": "pipeline",
        "source": str(tmp_path / "raw"),
        "fps": 10,
    }
    assert calls[2][1] is None  # a read
    assert result["job"]["dataset"] == "local/x"
    assert result["diagnostics"] == {"ok": True}


def test_the_default_channel_is_the_core_socket():
    from levi.agent import core

    assert load().exercise.__kwdefaults__["request"] is core.request
