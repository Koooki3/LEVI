"""The UI rules audit: its detectors work, and src/ has no hard-rule hit."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "ui_rules_audit", ROOT / "scripts" / "ui_rules_audit.py"
)
audit = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(audit)


def rules(name: str, text: str) -> list[str]:
    return [finding[0] for finding in audit.audit_text(name, text)]


def test_css_detectors() -> None:
    assert rules("a.css", ".x { transition: all 0.2s; }") == ["anim-all"]
    assert rules("a.css", ".x { transition: opacity 0.2s; }") == []
    assert rules("a.css", ".x { font-size: 11px; }") == ["tiny-text"]
    assert rules("a.css", ".x { font-size: 12px; }") == []
    assert rules("a.css", ".x:focus-visible { outline: none; }") == ["outline-none"]


def test_tsx_detectors() -> None:
    assert rules("a.tsx", "<div onClick={go}>x</div>") == ["click-div"]
    assert rules("a.tsx", '<div role="button" onClick={go}>x</div>') == []
    assert rules("a.tsx", "<button onClick={go}>x</button>") == []
    assert rules("a.tsx", 'content="width=device-width, user-scalable=no"') == [
        "zoom-disabled"
    ]
    assert rules("a.tsx", "{d.toLocaleTimeString()}") == ["locale-less"]


def test_src_has_no_hard_rule_hit() -> None:
    found = audit.audit_tree(ROOT / "src")
    hard = [
        f"{name}:{line}: {rule}" for rule, name, line, _ in found if rule in audit.HARD
    ]
    assert hard == [], (
        "UI hard rules (docs/DESIGN.md, Interaction rules):\n" + "\n".join(hard)
    )
