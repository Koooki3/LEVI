"""What the chunk-acceleration adapters need from the openpi FR3 client, checked on the source (stdlib only).

One implementation for two callers: ``install.py`` (before anything is copied) and ``run_collect_phase.py`` (every
start, because the client files are the user's and change without notice). Nothing here imports the client; it parses
the bytes it hashes, so the sha256 it returns is the sha256 of the text that was checked.

Every failure names the file, the invariant and the likely fix. The hooks it protects (design 2026-11-01, section 4):
A ``_query_policy``, B ``_clamp_action``, C ``_rtc_chunker``, D ``collector.main(scheduler_factory=..)``, E
``_run_episode`` / ``EvalLog`` / ``RolloutRecorder``.
"""

import ast
import hashlib
from pathlib import Path

# name -> path under the openpi checkout. rc = the client, rtcc = its RTC scheduler, coll = the collector, vrt = the
# installed VLASH runtime (reviewed bytes: VRT_REVIEWED_SHA256).
CLIENT_FILES = {
    "rc": "examples/fr3_local/run_robotiq_client.py",
    "rtcc": "examples/fr3_local/rtc_chunker.py",
    "coll": "examples/fr3_local/run_robotiq_client_collect_data.py",
    "vrt": "lab/vlash/runtime.py",
}

# The installed openpi/lab/vlash/runtime.py after the WP0 alignment (LEVI/integrations/vlash/runtime.py equals it).
# The VLASH scheduler of the phase clock is written against exactly these bytes; run_collect_phase.py refuses to start
# a VLASH run on any other. Change it only together with a review of PhaseVlashChunker.
VRT_REVIEWED_SHA256 = "bbedd1196a7d38722bd430f0f8441ebe46548ea607a90f8328163d5a4fc350b2"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _calls(node):
    """Dotted names of every call under ``node`` ('rtc.next_action', '_clamp_action', 'rc._run_episode')."""
    return {
        name
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and (name := _dotted(n.func))
    }


def _find(tree, kind, name):
    return next(
        (n for n in ast.walk(tree) if isinstance(n, kind) and n.name == name), None
    )


def _function(tree, name):
    return _find(tree, (ast.FunctionDef, ast.AsyncFunctionDef), name)


def _class(tree, name):
    return _find(tree, ast.ClassDef, name)


def _params(func):
    a = func.args
    return {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}


def _self_attrs(func):
    out = set()
    for n in ast.walk(func):
        targets = (
            n.targets
            if isinstance(n, ast.Assign)
            else [n.target]
            if isinstance(n, (ast.AnnAssign, ast.AugAssign))
            else []
        )
        for t in targets:
            if (
                isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name)
                and t.value.id == "self"
            ):
                out.add(t.attr)
    return out


def _module_names(tree):
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return names


def _need(ok, where, what, fix):
    if not ok:
        raise ValueError(f"{where}: {what} (the adapter wraps it; {fix})")


def _check_rc(tree, where):
    for name in (
        "_query_policy",
        "_run_episode",
        "_episode_loop",
        "_clamp_action",
        "_rtc_chunker",
        "_policy_payload",
        "_chunk_of",
        "_check_rtc_server",
        "wrap_to_pi",
        "EvalLog",
    ):
        _need(
            _find(tree, (ast.FunctionDef, ast.ClassDef), name) is not None,
            where,
            f"{name} is missing",
            "restore it or re-review the adapter",
        )
    log = _class(tree, "EvalLog")
    init = next(
        (
            n
            for n in log.body
            if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        ),
        None,
    )
    _need(
        init is not None and {"path", "header"} <= _self_attrs(init),
        where,
        "EvalLog.__init__ no longer sets self.path and self.header",
        "the trace directory and the session header hook need both",
    )
    run = _function(tree, "_run_episode")
    _need(
        {"scheduler_factory", "recorder"} <= _params(run),
        where,
        "_run_episode lost its scheduler_factory / recorder parameter",
        "VLASH and the episode hook need them",
    )
    _need(
        "_rtc_chunker" in _calls(run),
        where,
        "_run_episode no longer calls _rtc_chunker",
        "hook C replaces that module global",
    )
    loop = _calls(_function(tree, "_episode_loop"))
    for call in (
        "rtc.next_action",
        "rtc.action_adjusted",
        "rtc.reset",
        "recorder.stage",
        "_clamp_action",
        "_query_policy",
    ):
        _need(
            call in loop,
            where,
            f"_episode_loop no longer calls {call}",
            "hooks A/B/E and the schedulers depend on that call",
        )


def _check_rtcc(tree, where):
    names = _module_names(tree)
    for name in ("_Request", "WAIT_LIMIT_S", "MAX_SEED_DELAY", "RtcChunker"):
        _need(
            name in names,
            where,
            f"{name} is missing",
            "PhaseRtcChunker imports it",
        )
    init = next(
        (
            n
            for n in _class(tree, "RtcChunker").body
            if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        ),
        None,
    )
    _need(
        init is not None
        and {"_request", "execute_horizon", "period_s"} <= _self_attrs(init),
        where,
        "RtcChunker.__init__ no longer assigns _request, execute_horizon and period_s",
        "hook C builds the phase chunker from those three",
    )


def _check_coll(tree, where):
    main = _function(tree, "main")
    _need(
        main is not None and {"scheduler_factory", "server_validator"} <= _params(main),
        where,
        "main() lost scheduler_factory / server_validator",
        "VLASH is wired through them (hook D)",
    )
    rec = _class(tree, "RolloutRecorder")
    _need(rec is not None, where, "RolloutRecorder is missing", "hook E wraps it")
    methods = {n.name: _params(n) for n in rec.body if isinstance(n, ast.FunctionDef)}
    _need(
        "_session_metadata" in methods
        and "stage" in methods
        and "extra" in methods.get("close", set())
        and "detail" in methods.get("abort", set()),
        where,
        "RolloutRecorder no longer has _session_metadata, stage, close(extra) and abort(detail)",
        "the header, the aperture capture and the per-episode record need them",
    )
    _need(
        "rc._run_episode" in _calls(tree) and "rc.EvalLog" in _calls(tree),
        where,
        "main() no longer calls rc._run_episode and rc.EvalLog through the rc module",
        "hook E patches those module attributes; a direct import would bypass it",
    )


def _check_vrt(tree, where):
    for name in ("VlashChunker", "_Request", "vector", "check_server"):
        _need(
            _find(tree, (ast.FunctionDef, ast.ClassDef), name) is not None,
            where,
            f"{name} is missing",
            "PhaseVlashChunker is written against it",
        )


_CHECKS = {"rc": _check_rc, "rtcc": _check_rtcc, "coll": _check_coll, "vrt": _check_vrt}


def read_client(root):
    """{name: bytes} of the four client files under ``root`` (an openpi checkout); ValueError when one is missing."""
    out = {}
    for name, rel in CLIENT_FILES.items():
        path = Path(root) / rel
        if not path.is_file():
            raise ValueError(
                f"{path}: client file missing (is {root} an openpi checkout?)"
            )
        out[name] = path.read_bytes()
    return out


def check_client(root):
    """Parse and check the four client files; returns {name: sha256} of exactly the bytes checked."""
    shas = {}
    for name, data in read_client(root).items():
        where = CLIENT_FILES[name]
        try:
            tree = ast.parse(data)
        except SyntaxError as exc:
            raise ValueError(f"{where}: does not parse ({exc})") from exc
        _CHECKS[name](tree, where)
        shas[name] = sha256_bytes(data)
    return shas
