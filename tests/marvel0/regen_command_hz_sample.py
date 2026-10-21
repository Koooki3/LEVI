"""Regenerates ``src/components/marvel0/__tests__/marvel0-command-hz.sample.json``:
what the backend works out for each control rate (``command.control_summary``,
the warnings, the default RTC horizon) and the ``control`` block of the site
answer, on the sample site. The frontend test ``control-logic.test.ts`` works the
same numbers out with the page's own code and fails when the two differ.

    uv run --no-sync python tests/marvel0/regen_command_hz_sample.py

Nothing is started and nothing of the real machine is read; the site is the
temporary one of ``site_sample.py``. ``tests/marvel0/test_marvel0_command_hz_sample.py``
fails, naming this command, when the committed file differs from what this builds."""

import json
import tempfile
from pathlib import Path

from regen_sample import FOLDER, ROOT
from site_sample import make_site
from test_marvel0_command import PROMPT, model

from levi.marvel0 import command as C

SAMPLE = FOLDER / "marvel0-command-hz.sample.json"
__all__ = ["ROOT", "SAMPLE", "build"]

# (name, the form body without the model and the instruction)
CASES = [
    *[(f"default_{hz}hz", {"control_hz": hz}) for hz in C.CONTROL_HZ_OPTIONS],
    *[(f"open_loop_{hz}hz", {"control_hz": hz, "rtc": False}) for hz in (5, 20, 50)],
    ("long_episode_25hz", {"control_hz": 25, "max_steps": 1000, "rtc_horizon": 5}),
    ("short_episode_20hz", {"control_hz": 20, "max_steps": 120}),
    ("wide_horizon_20hz", {"control_hz": 20, "rtc_horizon": 8}),
    ("horizon_1_at_50hz", {"control_hz": 50, "rtc_horizon": 1}),
]


WIDTH = 80  # prettier's print width


def _scalar(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _lines(value, indent, head, tail) -> list:
    """``head`` + the value + ``tail`` as lines the way prettier prints JSON, so
    that ``format:check`` of the frontend accepts the file: an object is always
    open, keys kept in order; an array of plain values is on one line when it
    fits in 80 columns, else one value per line."""
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{pad}{head}{{}}{tail}"]
        out = [f"{pad}{head}{{"]
        for n, (key, item) in enumerate(value.items()):
            end = "," if n < len(value) - 1 else ""
            out += _lines(item, indent + 2, f"{_scalar(key)}: ", end)
        return [*out, f"{pad}}}{tail}"]
    if isinstance(value, list):
        if not value:
            return [f"{pad}{head}[]{tail}"]
        if not any(isinstance(item, (dict, list)) for item in value):
            one = f"{pad}{head}[{', '.join(_scalar(item) for item in value)}]{tail}"
            if len(one) <= WIDTH:
                return [one]
        out = [f"{pad}{head}["]
        for n, item in enumerate(value):
            out += _lines(item, indent + 2, "", "," if n < len(value) - 1 else "")
        return [*out, f"{pad}]{tail}"]
    return [f"{pad}{head}{_scalar(value)}{tail}"]


def build() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        site = make_site(Path(tmp))
        mdl = model()
        cases = []
        for name, body in CASES:
            form = C.validate(site, mdl, {"prompt": PROMPT, **body})
            preview = C.render(site, mdl, form)
            cases.append(
                {
                    "name": name,
                    "body": body,
                    "effective": {
                        "hz": form.control_hz,
                        "max_steps": form.max_steps,
                        "rtc": form.rtc,
                        "horizon": form.rtc_horizon,
                    },
                    "control": preview.control,
                    "warnings": [w["code"] for w in preview.warnings],
                    "flags": [
                        t
                        for t in preview.argv
                        if t.startswith(
                            ("--control-hz", "--max-step", "--rtc-exec", "--open-loop")
                        )
                    ],
                }
            )
        doc = {
            "schema": "levi.marvel0.command_hz_sample.v1",
            "about": "Written by tests/marvel0/regen_command_hz_sample.py from levi/marvel0/command.py.",
            "site_control": C.control_public(site),
            "cases": cases,
        }
    return "\n".join(_lines(doc, 0, "", "")) + "\n"


if __name__ == "__main__":
    SAMPLE.write_text(build())
    print(f"wrote {SAMPLE.relative_to(ROOT)}")
