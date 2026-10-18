# AERI external-agent bridge

[中文](AERI_EXTERNAL.zh-CN.md) · [AERI providers](AERI_PROVIDERS.md)

AERI can retain both local vLLM inference and an independently operated external
agent. `levi.automatic.external_bridge` provides the same HTTP surface used by
`VllmSubtaskVlm`; it does not run a model, start agents, contact robot/policy
services, execute answers, or write operator labels. Existing local-mode launch
configuration remains valid. Selecting an external bridge is an explicit launch
choice, not an automatic GPU fallback. This module is a software candidate;
adding it does not deploy it or establish real-robot validation.

Example invocation (documentation only; launching requires the normal service
ownership and, for a robot session, human authorization):

```sh
python -m levi.automatic.external_bridge serve frozen-model \
  --host 127.0.0.1 --port 18991 --served-model-name external-agent \
  --queue /path/to/owned/external-queue \
  --max-model-len 4096 --gpu-memory-utilization 0.6
```

Use the actual frozen model/name/port and vLLM flags expected by the caller.
Unknown flags are accepted as inert vLLM compatibility arguments, with a stderr
notice. They do not change the recognized `--host` or `--port`; abbreviated
options are not interpreted. `localhost` is pinned to `127.0.0.1` without DNS.
Only IPv4 loopback binds are accepted. Ports 5000, 5001, 5100, 7470 and 8000,
and privileged or invalid ports, are rejected. The queue must be explicitly
supplied and owned by the operator; do not put it in source captures or share it
with untrusted writers. This is a trusted local file interface, not authenticated
multi-user hosting. Queue storage is retained for audit and needs operator-managed
retention; no files are automatically deleted.

## HTTP and capacity

- `GET /health`: `{ "ok": true }`.
- `GET /v1/models`: OpenAI model list containing the served name.
- `POST /sleep?level=N`, `POST /wake_up`: acknowledge sleep/awake state;
  `GET /is_sleeping` reports it. There is no GPU allocation to unload.
- `POST /v1/chat/completions`: a non-streaming completion whose assistant content
  is the external `answer.json` text. `usage` is deliberately absent: unknown
  token/cost usage is not zero. Report actual external costs separately, tied to
  the request directory/completion ID.

Sleep rejects new completion admission (503); admitted requests continue waiting.
By default one request may wait at a time; concurrent excess receives 429 without
publishing a directory. `--max-pending` permits 1–16 concurrent requests, answered
independently by directory ID, with no FIFO ordering guarantee. There are at most
32 HTTP connection workers; excess connections are closed. Each connection closes
after its response. Request-body reading has a total deadline; request line and headers share a separate absolute deadline. A client disconnect does not cancel an admitted request.

| Setting | Default | Maximum |
| --- | --- | --- |
| `--max-request-bytes` | 32 MiB | 128 MiB |
| `--max-answer-bytes` | 1 MiB | 8 MiB |
| `--answer-timeout-s` | 30 seconds | 3600 seconds |
| `--request-timeout-s` | 60 seconds | 7200 seconds |
| `--read-timeout-s` | 10 seconds | 60 seconds |

All limits must be positive and finite. Oversized requests return 413, invalid
requests 400, answer timeouts 504, and invalid/unreadable published answers 502.
Timeouts follow the caller's unavailable-answer path; they are not judgments.
No HTTP streaming, remote image fetching or chunked request transfer is supported.

## Caller timeout binding

Startup enforces `answer_timeout_s + 2 * read_timeout_s < request_timeout_s`:
there is separate head/body read allowance and remaining response overhead. The
bridge default (30-second answer, 10-second reads, 60-second caller) fits AERI's
60-second default. `--request-timeout-s` declares the actual caller deadline; it
does not change the caller. Freeze the same value in the new job's
`plan.subtask.vlm.request_timeout_s` (not in the binding block). The bridge cannot
inspect a remote caller's configuration. Do not reuse an old frozen job with a
changed timeout or edit an existing experiment's configuration.

For a long external session, create a new job with this VLM plan fragment:

```json
{"provider_mode":"external","port":18991,"model":"frozen-model","served_name":"external-agent",
 "min_model_len":4096,"max_model_len":4096,"gpu_memory_utilization":0.6,"request_timeout_s":600}
```

`subtask.vlm.provider_mode` may explicitly declare `external` or `local`. It
records the configured answer provider in a new job's frozen plan and plan SHA;
omitting it or using null preserves the previous plan representation. The run
page shows “Configured answer provider” from the digest-checked plan. Older
records, missing/invalid declarations and older servers without this snapshot
field show “Unknown”. This declaration neither selects a backend nor verifies
the responding service's identity, and changes no model, GPU budget or service
lifecycle. Model names, argv and usage are not used to guess it. The page stays
read-only, and provider judgements are not operator labels. Job schema minor 3
adds this optional field.


Its binding `subtask_vlm` block can take the following form. Paths and resource
values are illustrative: preserve the reviewed policy-memory requirement and
shared GPU lock for the actual installation, and use an absolute interpreter
with LEVI installed. These examples authorize no launch or robot operation.

```json
{
  "subtask_vlm": {
    "base_url": "http://127.0.0.1:18991",
    "model": "external-agent",
    "service": {
      "unit": "levi-aeri-vlm-external",
      "argv": ["/path/to/venv/bin/python", "-m", "levi.automatic.external_bridge",
        "serve", "frozen-model", "--host", "127.0.0.1", "--port", "18991",
        "--served-model-name", "external-agent", "--queue", "/path/to/owned/queue",
        "--max-model-len", "4096", "--gpu-memory-utilization", "0.6",
        "--request-timeout-s", "600", "--answer-timeout-s", "540"],
      "gpu_lock": "/path/to/owned/gpu.lock",
      "need_policy_mib": 12000,
      "ready_timeout_s": 1800
    }
  }
}
```

The 540-second wait is refused unless the declared caller deadline is enlarged
(e.g. 600 seconds). A disconnected client is not reliably observable during file
waiting; its slot remains occupied only until the bounded answer deadline. An
`answer_ready` result includes `consumption: "unknown"`: it means valid content
was prepared, never that the client received or acted on it. Late answers remain
timeout artifacts. Filesystem/OS stalls are not a hard real-time guarantee.

## File protocol

Every admitted valid request gets a newly created `request-*` directory. Names
are not reset on restart, and prior directories/answers are never reused.
The bridge writes `request.json` (image URLs replaced by relative filenames),
`prompt.txt`, `schema.json` (the full `response_format`), and ordered
`img_1.png`, `img_2.jpg`, etc. Only strict base64 `data:image/png`, `image/jpeg`
and `image/webp` inputs are accepted. Image bytes are preserved, not interpreted.

**Consumers must ignore every directory without `READY`.** This JSON marker is
published atomically only after all request files have been closed. A failed
publication can leave an incomplete unmarked directory; it is never an actionable
request. Publication guarantees visibility, not persistence across power loss.

An independently authorized agent reads a ready request and its schema, then
writes its JSON answer to a temporary file in that same directory and atomically
renames it to `answer.json`. The answer file is the assistant content itself,
not an OpenAI completion envelope. Publish once, as UTF-8 JSON without Markdown
fences, duplicate keys, NaN or Infinity. The bridge rejects oversized files,
symlinks, non-regular files and malformed JSON. It does not execute answers or
validate task-specific semantics; AERI still owns schema/whitelist/template and
operator gates. Do not modify published request files or substitute directories.

`result.json` records `answer_ready`, `timeout` or `invalid_answer` when writable.
Consumers must not pick requests with a result marker. After timeout, late answers
are audit artifacts only and do not affect another request. The earlier lab `wait_request` consumer expects sequence-based directory names
and is not directly compatible with this protocol; use a READY-aware consumer.
Existing lab tools and frozen experiment configurations are not modified.
A server restart does
not resume old waiters: reconcile old READY folders against the previous session
before assigning new work. Multiple external workers need their own exclusive
claim coordination; the bridge does not assign or spawn them. Do not infer that
an answer is an operator success/failure label.

## Offline verification

`tests/automatic/test_external_bridge.py` exercises publication, restart-safe names,
inline-image validation, bounded regular-file reads, injected-clock deadlines,
capacity/sleep behavior, absent usage, CLI boundaries and HTTP contracts using
in-memory streams. It opens no listening socket and imports no GPU framework.
Run it only under the repository's current test/resource and robot-safety gates.

Every request also includes `BRIEF.md`, closed before READY is published. An
independently authorized dispatcher may refer the answerer to this file rather
than repeating the common task sheet. It identifies the current request and
points to the authoritative messages, schema and images; it adds no task-specific
success policy or robot permission. This reduces duplicated dispatch text, but
no token, cost or answer-quality improvement has been measured for this candidate.

## Optional self-report evidence (persistence only)

A responder may publish a fixed `report.json` in the current request directory,
then publish `answer.json` last. Both publications use temporary files and atomic
rename. An answer-only responder remains compatible. Reports are optional metadata;
a missing, malformed or mismatched report never changes the business answer,
operator labels or control state. No old sessions, proxy logs or `agent_notes.txt`
files are discovered or imported.

```json
{
  "schema": "levi.aeri.external_report.v1",
  "request_id": "request-example",
  "answer_sha256": "<SHA-256 of the exact answer.json UTF-8 bytes>",
  "agent_id": "worker-7",
  "agent_notes": "The visible object is resting on the target and outside the gripper.",
  "elapsed_ms": 18400,
  "tokens": {"input_new": 1200, "output": 240, "cache_read": null, "cache_write": null}
}
```

Only schema, request ID and answer digest are required. The other fields may be
omitted or null, except that `tokens`, when supplied, must be an object (its four
fields may independently be omitted/null). Unknown keys invalidate the report.
The request ID must equal this request's directory/completion ID. The answer digest
covers exact bytes, including whitespace and newlines: do not strip text, normalize
Unicode or serialize parsed JSON again. The bridge decodes UTF-8 and preserves the
content string; the client re-encodes that full string to verify the same digest.
This binding establishes content/request correspondence, not author authenticity,
billing accuracy, answer correctness or operator approval.

Limits: report 16 KiB; `agent_notes` 4096 UTF-8 bytes; `agent_id` 1–64 ASCII
letters/digits or `_ . : -`; `elapsed_ms` integer 0–86400000; each token field an
integer 0–9007199254740991. Booleans, numeric strings, fractional counts, negative
counts, nonfinite numbers, duplicate JSON keys and invalid UTF-8 are rejected.
The file must be regular, bounded and not a symlink. Only that fixed filename is
read: no report-supplied paths or remote resources. Notes must be already-public
short observations/reasons intended for the operator, not private internal reasoning.
Identity, time and all four token counts are self-reported. Missing counts stay
unknown; true zeros remain zero. Cache dimensions are not summed, inferred from
other counts or converted to price.

At each answer poll, the bridge snapshots the report before checking the answer.
The snapshot used when the answer is accepted is final: the bridge never waits for
metadata, refreshes a completed response or backfills a late report. A report
published after that snapshot stays unknown. Report-first/answer-last is the writer
protocol, not a claim that filesystem timestamps authenticate publication order.
Timed-out requests cannot transfer reports or answers to a later request.

The completion has an additive `levi_external` envelope with schema
`levi.aeri.external_evidence.v1`, `bridge_wait_ms`, `report_status`,
`report_source: "external_self_reported"`, and normalized `report` or null.
Statuses are `reported`, `unavailable`, `invalid`, `mismatch`, and
`unsupported_version`. `bridge_wait_ms` is observed with a monotonic clock from
immediately after READY publication until answer acceptance. It differs from
self-reported `elapsed_ms` and the client's existing HTTP `latency_ms`. Standard
OpenAI `usage` remains absent; self-reported counters are never copied into the
standard prompt/completion token fields. Missing local/proxy reports remain unknown;
proxy-log ingestion is not implemented.

The client revalidates the extension, including ID/digest against the full answer,
and records `binding_verified` (content association only). Invalid extensions are
discarded without changing answer handling. `VlmReply` passes the evidence to a
separate `<sequence>-request-evidence.json` under the run's `subtasks` directory;
the existing request record keeps only its `request_evidence` file/digest reference.
The new file does not end in `-detail.json`, so existing public note expansion does
not expose it. The existing plan projection also does not expose request evidence.
**This first slice adds persistence only, not new API/UI free-text visibility.**

The independent evidence file keeps the actual client-generated prompt and answer
as bounded 20000-character display prefixes, their full UTF-8 byte counts and
SHA-256 digests, and explicit `prompt_text_truncated` / `raw_content_truncated`
flags. A truncated prefix cannot independently verify the full-content digest.
These bounded text fields plus the report fit within the existing 1 MiB sidecar
budget for normal AERI frame references. Report text cannot override the prompt.

`evidence_context` is host-owned: inventory has null round/episode IDs; directional
plans use the current authoritative `round_id`; checks additionally use the current
episode ID and require agreement with the locally constructed request record and
loop. Missing/inconsistent association is null, never guessed from an integer
round or provider text. Historic evidence is not backfilled. A later read-only UI
must implement exact round-label/member visibility before exposing this new file;
this change does not alter the existing API or its historical visibility behavior.

### Read-only request evidence

The run window projects private request evidence only after the server verifies
its host-owned exact round ID against the journal and the latest human round
label (`success` or `failure`). Checks must also match the started episode and
that round’s forward/reverse members. Reverse checks remain hidden until their
members are recorded. Inventory, discarded rounds, members without a label,
legacy records without exact context and damaged or conflicting associations stay
hidden. A later discard hides future responses; it cannot retract already viewed
content. Numeric round grouping and self-reported identity never grant access.

The existing subtask response carries the projection; there is no new fetch or
write control. Hidden rows contain no new free text or private sidecar reference.
Labels and evidence use the bounded, no-symlink reader. The projection includes at
most the latest 16 questions, reads at most 4 MiB (reserving the single-file limit
before each read), and returns at most 64,000 free-text characters. A truncation
flag explains omitted evidence/text. Complete journal association precedes display
limits. Existing sidecar limits are unchanged.

The answerer ID and notes are self-reported, not authenticated identity or operator
labels. New input, output, cache-read and cache-write token counts remain independent:
unknown is unknown and zero is zero. Client request latency, bridge wait and
self-reported elapsed time describe different intervals. No billing is inferred.
Prompt/answer text is rendered as plain text. Byte lengths and SHA-256 digests
refer to complete pre-truncation content; displayed prefixes (and path-redacted
text) cannot independently verify those full-content digests.


## Explicit redacted local-agent export

`python -m levi.automatic.local_log_import` converts **one explicitly staged,
already-redacted export** to the existing optional `external_report.v1` object.
This is LEVI's interchange format, not a parser for Codex or Claude native
sessions, JSONL transcripts, tool logs or billing records. There is no automatic
session discovery, directory scan, queue publication, evidence backfill or label
write. A raw private session is not an accepted input.

Prepare an owned, separate staging directory containing exactly the two input
files below (other files are ignored, not scanned):

- `agent-export.json`: a strict UTF-8 JSON object with exactly `schema`, `source`,
  `redacted` and `report`. `schema` is `levi.aeri.agent_log_export.v1`; `source` is
  one of `codex`, `claude`, `other`, `synthetic`, declared by the exporter and not
  authenticated. `redacted` must be literal `true`. `report` follows the optional
  report contract above, including exact request ID and answer SHA-256.
- `answer.json`: the original raw assistant-content JSON used by the bridge,
  **not an OpenAI completion response** or Markdown. Preserve every UTF-8 byte,
  whitespace and newline; the importer does not strip or reserialize it. The
  object must not contain `choices` or `usage`. This importer checks the JSON
  transport boundary; the normal provider retains inventory/plan/check semantic
  validation, including the requested facts and whitelist.

Before staging, the person preparing the export must remove credentials, private
paths, unrelated conversation, tool output and private reasoning from all exported
fields. Only an intentionally shareable summary belongs in `agent_notes`.
`redacted: true` is an assertion, **not proof of successful sanitization**; this
command cannot detect every secret. It never opens a path named inside the JSON.
Keep the reviewed export separately if its declared source is needed for audit:
source/redaction declarations are not added to the existing report schema.

```sh
python -m levi.automatic.local_log_import \
  --export-dir /absolute/owned/redacted-export \
  --request-id request-example
```

The request ID must come independently from the intended bridge request. Success
prints only the normalized report JSON to stdout; failure returns status 2 with
a fixed error code on stderr and no report. No output file is created. The result
can be consumed by the existing optional `report.json` protocol, after the normal
review and publication procedure; this command does not publish it or make late
reports visible. Existing exact-round human-label visibility rules remain intact.
It never changes standard provider `usage`, answers, timing or control decisions.

The directory must be absolute, have no hidden components and no symlink at any
level. Only the fixed filenames are opened, with no symlink following; files must
be regular, owned by the current user and have one hard link. Export input is at
most 20 KiB, answer at most 1 MiB, JSON depth at most 16 and output report at most
16 KiB. Duplicate JSON keys, nonfinite numbers, unknown export/report fields,
missing redaction, mismatched IDs/hashes and malformed reports fail closed.
Tokens retain independent missing/null versus zero values; booleans are not
integers. Self-reported identity, elapsed time and tokens are neither authenticated
identity nor billing facts. No token count is inferred from a model or transcript.

A fully synthetic export is tracked at
`tests/automatic/fixtures_local_import/local_agent_export_v1.json`; its exact answer bytes are
specified by `ANSWER` in `tests/automatic/test_local_log_import.py`. It is a format
example and regression fixture, not measured external-agent evidence.
