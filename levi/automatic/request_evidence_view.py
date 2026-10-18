"""Fail-closed, read-only projection of private request evidence.

The caller supplies the API's bounded, no-symlink reader. Provider identities,
round numbers and UI grouping never authorize disclosure.
"""

from __future__ import annotations

import re

from .external_report import strict_json

MAX_REQUESTS = 16
MAX_READ_BYTES = 4 * (1 << 20)
MAX_FILE_BYTES = 1 << 20
MAX_TEXT_CHARS = 64000

KINDS = {"plan-forward", "plan-reverse", "check-forward", "check-reverse"}
PRIVATE = re.compile(r"subtasks/([0-9]{6,})-request-evidence\.json")
PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")


def without_references(value):
    """Never publish the new private sidecar reference through legacy detail."""
    if isinstance(value, dict):
        return {
            k: without_references(v)
            for k, v in value.items()
            if k != "request_evidence"
        }
    if isinstance(value, list):
        return [without_references(v) for v in value]
    return value


def associations(events):
    releases, started, current = {}, {}, None
    run_id = None
    invalid = False
    for event in events:
        note = getattr(event, "note", None)
        if getattr(event, "record", None) != "note" or note is None:
            continue
        if note.code not in ("subtask_round_released", "subtask_started"):
            continue
        try:
            event_run = getattr(event, "run_id", None)
            if not isinstance(event_run, str) or not event_run:
                raise TypeError()
            if run_id is not None and event_run != run_id:
                raise ValueError()
            run_id = event_run
            d = strict_json(note.detail)
            if not isinstance(d, dict):
                raise TypeError()
            round_no = d.get("round")
            if type(round_no) is not int or round_no < 1:
                raise ValueError()
            if note.code == "subtask_round_released":
                rid = d.get("round_id")
                expected = f"{run_id}:round{round_no}:s{event.sequence_no}"
                if rid != expected or rid in releases:
                    raise ValueError()
                releases[rid] = event.sequence_no
                current = (rid, round_no)
            else:
                eid, loop = d.get("episode_id"), d.get("loop")
                if (
                    current is None
                    or round_no != current[1]
                    or loop not in ("forward", "reverse")
                ):
                    raise ValueError()
                role = "forward" if loop == "forward" else "reset"
                if (
                    not isinstance(eid, str)
                    or not re.fullmatch(
                        re.escape(f"{run_id}.{role}.") + r"[0-9]{4,}", eid
                    )
                    or int(eid.rsplit(".", 1)[1]) < 1
                    or eid in started
                ):
                    raise ValueError()
                started[eid] = (current[0], loop, event.sequence_no)
        except (ValueError, TypeError):
            invalid = True
    return ({}, {}) if invalid else (releases, started)


def labels_of(read):
    """Latest label wins. A members row alone cannot grant visibility."""
    try:
        raw = read(("labels", "round_label.jsonl"))
        # A partial final record might revoke permission: do not use an older label.
        if raw and not raw.endswith(b"\n"):
            return {}
        labels = {}
        for line in raw.splitlines():
            d = strict_json(line.decode("utf-8"))
            if (
                not isinstance(d, dict)
                or d.get("schema") != "levi.aeri.round_label.v1"
                or d.get("kind") != "round_label"
            ):
                return {}
            rid, record, members = d.get("round_id"), d.get("record"), d.get("members")
            if not isinstance(rid, str) or not rid or not isinstance(members, dict):
                return {}
            if not members.keys() <= {"forward", "reverse"}:
                return {}
            for values in members.values():
                if (
                    not isinstance(values, list)
                    or not all(isinstance(v, str) and v for v in values)
                    or len(set(values)) != len(values)
                ):
                    return {}
            found = labels.setdefault(
                rid, {"value": None, "forward": [], "reverse": []}
            )
            if record == "label":
                if (
                    d.get("value") not in ("success", "failure", "discarded")
                    or not isinstance(d.get("by"), str)
                    or not PRINCIPAL.fullmatch(d["by"])
                    or not members.get("forward")
                ):
                    return {}
                found["value"] = d["value"]
                found["forward"] = members["forward"]
                # Production post-run labels include reverse; terminal labels precede it.
                if members.get("reverse"):
                    found["reverse"] = members["reverse"]
            elif record == "members" and set(members) == {"reverse"}:
                found["reverse"] = members["reverse"]
            else:
                return {}
        return labels
    except (OSError, ValueError, TypeError):
        return {}


def _number(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def _text(value, limit):
    return value if isinstance(value, str) and len(value) <= limit else None


def _public(data, request):
    # Explicit allowlist: neither file names nor arbitrary provider keys escape.
    ext = data.get("external_evidence")
    ext = (
        ext
        if isinstance(ext, dict)
        and ext.get("schema") == "levi.aeri.external_evidence.v1"
        and ext.get("report_source") == "external_self_reported"
        else {}
    )
    report = ext.get("report")
    report = (
        report
        if isinstance(report, dict)
        and ext.get("schema") == "levi.aeri.external_evidence.v1"
        and ext.get("report_source") == "external_self_reported"
        and ext.get("binding_verified") is True
        and ext.get("report_status") == "reported"
        and report.get("schema") == "levi.aeri.external_report.v1"
        and report.get("request_id") == data.get("vlm_request_id")
        and report.get("answer_sha256") == data.get("answer_content_sha256")
        else {}
    )
    tokens = report.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    return {
        "agent_id": _text(report.get("agent_id"), 64),
        "agent_notes": _text(report.get("agent_notes"), 4096),
        "tokens": {
            key: _number(tokens.get(key))
            for key in ("input_new", "output", "cache_read", "cache_write")
        },
        "client_latency_ms": _number(request.get("latency_ms")),
        "bridge_wait_ms": _number(ext.get("bridge_wait_ms")),
        "self_reported_elapsed_ms": _number(report.get("elapsed_ms")),
        "prompt_text": _text(data.get("prompt_text"), 20000),
        "raw_content": _text(data.get("raw_content"), 20000),
        "prompt_text_truncated": data.get("prompt_text_truncated") is True,
        "raw_content_truncated": data.get("raw_content_truncated") is True,
        "prompt_bytes": _number(data.get("prompt_bytes")),
        "answer_bytes": _number(data.get("answer_bytes")),
        "prompt_sha256": _text(data.get("prompt_sha256"), 64),
        "answer_content_sha256": _text(data.get("answer_content_sha256"), 64),
    }


def project(events, rows, run_dir, read, verified):
    """Bounded displayed rows, authorized against the COMPLETE journal."""
    releases, started = associations(events)
    used_bytes = 0
    truncated = False

    def bounded_read(parts):
        nonlocal used_bytes, truncated
        # Reserve the existing API single-file limit BEFORE doing any I/O.
        if used_bytes + MAX_FILE_BYTES + 1 > MAX_READ_BYTES:
            truncated = True
            raise OSError("request evidence read budget exhausted")
        used_bytes += MAX_FILE_BYTES + 1
        raw = read(parts)
        used_bytes -= max(0, MAX_FILE_BYTES + 1 - len(raw))
        if len(raw) > MAX_FILE_BYTES:
            truncated = True
            raise OSError("request evidence file too large")
        return raw

    labels = labels_of(bounded_read) if releases else {}
    candidates = [
        r
        for r in rows
        if r["code"]
        in (
            "subtask_inventory",
            "subtask_inventory_rejected",
            "subtask_plan",
            "subtask_plan_refused",
            "subtask_check",
        )
    ]
    truncated = truncated or len(candidates) > MAX_REQUESTS
    remaining_text = MAX_TEXT_CHARS
    out = []
    for row in candidates[-MAX_REQUESTS:]:
        code, d = row["code"], row.get("detail") or {}
        if code not in (
            "subtask_inventory",
            "subtask_inventory_rejected",
            "subtask_plan",
            "subtask_plan_refused",
            "subtask_check",
        ):
            continue
        result = {
            "seq": row["seq"],
            "visibility": "hidden",
            "context": None,
            "report": None,
        }
        out.append(result)
        if not labels or not releases:
            continue
        name, digest = (
            (d.get("plan_file"), d.get("sha256"))
            if code == "subtask_plan"
            else (d.get("file"), d.get("file_sha256"))
        )
        try:
            raw, _ = verified(bounded_read, run_dir, name, digest)
            request = strict_json(raw) if raw is not None else None
            if code == "subtask_plan" and isinstance(request, dict):
                request = request.get("request")
            if not isinstance(request, dict):
                continue
            ref = request.get("request_evidence")
            if (
                not isinstance(ref, dict)
                or not isinstance(ref.get("file"), str)
                or not PRIVATE.fullmatch(ref["file"])
            ):
                continue
            if int(PRIVATE.fullmatch(ref["file"])[1]) != row["seq"]:
                continue
            raw, _ = verified(
                bounded_read, run_dir, ref["file"], ref.get("file_sha256")
            )
            data = strict_json(raw) if raw is not None else None
            if (
                not isinstance(data, dict)
                or data.get("schema") != "levi.aeri.request_evidence.v1"
            ):
                continue
            ctx = data.get("evidence_context")
            if not isinstance(ctx, dict) or set(ctx) != {
                "kind",
                "round_id",
                "episode_id",
            }:
                continue
            kind, rid, eid = ctx["kind"], ctx["round_id"], ctx["episode_id"]
            if (
                not isinstance(kind, str)
                or kind not in KINDS
                or not isinstance(rid, str)
                or rid not in releases
            ):
                continue
            # Require this precise release's journal interval; never infer identity from it.
            seq = row["seq"]
            release = releases[rid]
            if release >= seq or any(
                release < other < seq for other in releases.values()
            ):
                continue
            loop = kind.split("-")[1]
            if request.get("loop") != loop or d.get("loop") != loop:
                continue
            expected = (
                "check"
                if code == "subtask_check"
                else "plan"
                if code in ("subtask_plan", "subtask_plan_refused")
                else None
            )
            if not kind.startswith(f"{expected}-"):
                continue
            label = labels.get(rid, {})
            if label.get("value") not in ("success", "failure"):
                continue
            # Validate ALL listed members, including cross-round/duplicate ownership.
            if any(
                started.get(member, (None, None))[:2] != (rid, direction)
                for direction in ("forward", "reverse")
                for member in label[direction]
            ):
                continue
            if kind.startswith("check-"):
                if (
                    not isinstance(eid, str)
                    or eid != request.get("episode_id")
                    or eid != d.get("episode_id")
                    or eid not in label[loop]
                    or started.get(eid, (None, None))[:2] != (rid, loop)
                    or started[eid][2] >= seq
                ):
                    continue
            elif eid is not None:
                continue
            report = _public(data, request)
            report["display_text_truncated"] = False
            for field in ("agent_notes", "prompt_text", "raw_content"):
                text = report[field]
                if text is not None:
                    report[field] = text[:remaining_text]
                    remaining_text -= len(report[field])
                    if len(report[field]) != len(text):
                        report["display_text_truncated"] = True
                        truncated = True
            result.update(visibility="visible", context=ctx, report=report)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return out, truncated
