"""The launch guide behind ``GET /api/levi/automatic/setup-guide`` (T-API-1).

An ordered list of ``SetupStep`` (the interface contract, §3) that tells an
operator what to prepare before a real session and whether it is ready:

- **manual steps** (emergency stop within reach, the arm released and
  enabled in its web interface, the objects placed): fixed texts, mode
  ``copy``, status ``todo``: nothing can see them, a person does them;
- **native steps** (the product, the live service, the local model server):
  status from the read-only probes of ``levi.setup.probes`` (a lookup in the
  kernel's table of listening ports; no connection is made);
- **recipe steps**: the commands of the operator guide (``setup.md``) as the
  recipe registry of ``levi.setup.recipes`` records them
  (``LEVI_SETUP_RECIPES``, ``LEVI_SETUP_DOC``). A recipe's ``command`` is
  the guide's text *as it is now*, shown only while its hash still matches
  the recorded one; when the guide changed the step says so
  (``warn``) and shows no command.

**Nothing here runs a command.** Every step is shown, none is executed: a
recipe marked ``execute`` is reported as ``copy`` (this version has no route
that runs anything). A probe that fails makes its steps ``unknown``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from levi.setup import recipes as recipe_book

RECIPES_ENV = recipe_book.ENV_RECIPES
DOC_ENV = recipe_book.ENV_DOC
MAX_DOC_BYTES = recipe_book.MAX_FILE_BYTES
_PATH = re.compile(r"(?<![\w/.:-])/(?:[A-Za-z0-9_.~+@%=,-]+/)*[A-Za-z0-9_.~+@%=,-]+")

WHY = {
    1: (
        "Read-only check; nothing changes.",
        "只读检查，不改变任何东西。",
    ),
    2: (
        "Starts or stops a LEVI, model-server or recorder service.",
        "启动或停止 LEVI、模型服务或记录器。",
    ),
    3: (
        "Starts the robot control stack or the policy server.",
        "启动机械臂控制栈或策略服务。",
    ),
    4: (
        "Can make the robot or the gripper move.",
        "可能让机械臂或夹爪运动。",
    ),
}

MANUAL_BEFORE = (
    {
        "id": "estop",
        "title": ("Emergency stop within reach", "急停按钮触手可及"),
        "level": 4,
        "why": (
            (
                "A person stands next to the robot with a hand on the emergency "
                "stop for the whole session; no software step replaces it."
            ),
            "整个过程中有人在机械臂旁，手放在急停上；任何软件步骤都不能代替它。",
        ),
    },
    {
        "id": "arm-enabled",
        "title": (
            "Arm released and enabled in its web interface",
            "在机械臂网页界面中解锁并启用",
        ),
        "level": 3,
        "why": (
            (
                "The brakes are released and the control interface is enabled by "
                "hand in the arm's own web page, as the operator guide says."
            ),
            "按操作手册，在机械臂自己的网页界面里手动解除刹车并启用控制接口。",
        ),
    },
)
MANUAL_AFTER = (
    {
        "id": "scene-objects",
        "title": (
            "Objects placed as the Initial State Contract says",
            "按初始状态契约摆放物体",
        ),
        "level": 4,
        "why": (
            (
                "The first episode starts from this scene; a person puts it "
                "there and checks it before the run starts."
            ),
            "第一个片段从这个场景开始；由人摆好并在启动前检查。",
        ),
    },
)
# (id, title, ports that must listen, level)
NATIVE = (
    (
        "product",
        ("LEVI product service", "LEVI 产品服务"),
        (7860, 7861),
        2,
        (
            "The page you are using and its core.",
            "你正在使用的页面及其核心。",
        ),
    ),
    (
        "live-service",
        ("Live annotation service", "后台实时标注服务"),
        (7881,),
        2,
        (
            "Judges the end of each episode (the online judge).",
            "判定每个片段的结束（在线判定）。",
        ),
    ),
    (
        "vllm",
        ("Local vision model server", "本地视觉模型服务"),
        (8100,),
        2,
        (
            "The model the live service asks.",
            "实时服务所调用的模型。",
        ),
    ),
)


def _scrub(text) -> str:
    return _PATH.sub("<path>", str(text))[:400]


def _step(
    step_id,
    title,
    level,
    mode,
    why,
    status,
    *,
    command=None,
    detail=None,
    requires=(),
):
    out = {
        "id": step_id,
        "title": {"en": title[0], "zh": title[1]},
        "level": level,
        "mode": mode,
        "why": {"en": why[0], "zh": why[1]},
        "status": status,
    }
    if command:
        out["command"] = command
    if detail:
        out["detail"] = _scrub(detail)
    if requires:
        out["requires"] = list(requires)
    return out


def probe_status() -> dict:
    """The read-only sample of ``levi.setup.probes`` (``{}`` when it fails)."""
    try:
        from levi.setup import probes

        value = probes.status()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001 - a failing probe is "unknown", never an error
        return {}


def listening_ports(status: dict) -> set | None:
    """The ports in LISTEN state, or ``None`` when the probe could not say."""
    block = status.get("ports") if isinstance(status, dict) else None
    if not isinstance(block, dict) or block.get("state") != "ok":
        return None
    return {
        row["port"]
        for row in block.get("ports", [])
        if isinstance(row, dict) and row.get("listening")
    }


def _ports_status(wanted, listening) -> str:
    if listening is None:
        return "unknown"
    return "ok" if set(wanted) <= listening else "todo"


def _read_guide(doc: str):
    try:
        path = Path(doc).expanduser()
        if path.stat().st_size > MAX_DOC_BYTES:
            return None, "the guide is too large"
        return recipe_book.parse_guide(path.read_text(encoding="utf-8-sig")), None
    except (OSError, UnicodeDecodeError) as exc:
        return (
            None,
            f"cannot read the guide ({getattr(exc, 'strerror', None) or type(exc).__name__})",
        )


def _recipe_steps(listening) -> list:
    recipes_file = os.environ.get(RECIPES_ENV, "").strip()
    doc = os.environ.get(DOC_ENV, "").strip()
    if not recipes_file or not doc:
        return [
            _step(
                "recipes-not-configured",
                (
                    "Startup commands come from the operator guide",
                    "启动命令来自操作手册",
                ),
                1,
                "copy",
                (
                    (
                        f"Set {RECIPES_ENV} and {DOC_ENV} to show the guide's "
                        "commands here, checked against their recorded hashes."
                    ),
                    (
                        f"设置 {RECIPES_ENV} 和 {DOC_ENV} 后，这里会显示手册里的命令，"
                        "并与记录的哈希核对。"
                    ),
                ),
                "unknown",
            )
        ]
    try:
        book = recipe_book.load(recipes_file)
    except recipe_book.RecipeError as exc:
        return [
            _step(
                "recipes-unreadable",
                ("Recipe file cannot be used", "配方文件不可用"),
                1,
                "copy",
                WHY[1],
                "warn",
                detail=str(exc),
            )
        ]
    guide, problem = _read_guide(doc)
    out = []
    for recipe in book.recipes:
        status, detail, command = "unknown", None, None
        finding = recipe_book.check_recipe(guide, recipe) if guide else None
        if guide is None:
            detail = problem
        elif finding.status != "ok":
            status = "warn"
            detail = (
                f"the guide changed since this recipe was recorded "
                f"({finding.status}): review it; no command is shown. "
                + "; ".join(finding.reasons[:2])
            )
        else:
            sections = guide.by_number(recipe.source.section)
            found = (
                recipe_book.excerpt(
                    sections[0], recipe.source.block, recipe.source.pick
                )
                if sections
                else None
            )
            command = found.text if found else None
            if recipe.listens:
                status = _ports_status(recipe.listens, listening)
            elif recipe.ui != "native":
                status = "todo"
        mode = "native" if recipe.ui == "native" else "copy"
        if recipe.ui == "execute":
            detail = (
                detail or ""
            ) + " (the page does not run commands in this version)"
        if recipe.ui == "native":
            command = None
        why = WHY[recipe.risk] if not recipe.note else (recipe.note, recipe.note)
        out.append(
            _step(
                recipe.id,
                (recipe.title, recipe.title),
                recipe.risk,
                mode,
                why,
                status,
                command=command,
                detail=detail.strip() if detail else None,
                requires=recipe.requires,
            )
        )
    for rid, text in book.problems[:10]:
        out.append(
            _step(
                f"recipe-problem-{len(out)}",
                (f"Recipe {rid or '?'} left out", f"配方 {rid or '?'} 已略去"),
                1,
                "copy",
                WHY[1],
                "warn",
                detail=text,
            )
        )
    return out


def steps(status: dict | None = None) -> list:
    """The ordered ``SetupStep`` list. ``status``: a sample of the probes
    (taken here when omitted)."""
    sample = probe_status() if status is None else status
    listening = listening_ports(sample)
    out = [
        _step(m["id"], m["title"], m["level"], "copy", m["why"], "todo")
        for m in MANUAL_BEFORE
    ]
    for step_id, title, ports, level, why in NATIVE:
        out.append(
            _step(
                step_id,
                title,
                level,
                "native",
                why,
                _ports_status(ports, listening),
                detail=None
                if listening is not None
                else "the port probe could not read the kernel's table",
            )
        )
    out += _recipe_steps(listening)
    out += [
        _step(m["id"], m["title"], m["level"], "copy", m["why"], "todo")
        for m in MANUAL_AFTER
    ]
    return out
