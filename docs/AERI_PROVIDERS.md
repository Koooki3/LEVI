# Automatic evaluation model providers

[中文](AERI_PROVIDERS.zh-CN.md) · [Automatic pipeline](AUTOMATIC_PIPELINE.md)

This provider executes a real predicate model call through the existing live online Judge. It does not generate robot actions. A file response is useful only after the complete AERI contract, current retained evidence, run/plan identity and deadline have been checked. A transport test is not evidence that a model or robot is ready.

## Start with no reset policy

The existing `human_assisted` / `operator_attested` path retains the operator scene gate and the forward C5 final-state judgement. It needs no scene model worker. Do not add a reset policy to get past a missing provider. A reset model instead reads the confirmed Initial State Contract (ISC); `generic-final` remains exclusively the forward final-state rule.

The new worker has `scene` and `reset_judgement` capabilities. **It has no events capability.** The existing FileEvents request has no observation frames, so this worker cannot implement visual events. A model-worker job must explicitly freeze `termination.allow_early_stop: false` in a new plan. A job retaining the old true default is blocked; this implementation never rewrites it silently. A future event channel or longer model deadline requires a new reviewed plan/spec and its own tests.

## Freeze a complete model specification

`levi.automatic.provider_model.FrozenSpec` reads `levi.aeri.model_spec.v1`. A valid file contains all of these fields:

| Field | Required source and check |
| --- | --- |
| `id`, `version`, `run_id`, `plan_sha256` | Explicit spec revision and exact frozen job identity. |
| `contract` | Complete confirmed ISC, including required/optional predicates and the evidence/view rules. |
| `definitions` | Every required and optional predicate has an operator-supplied physical definition. Name-based defaults are forbidden. Objects, locations, tolerances and visibility rules come from this source. |
| `source.ref`, `source.sha256` | Operator's frozen source reference and canonical SHA256 of `{"contract": ..., "definitions": ...}`. The hash binds the content; it does not certify physical safety or substitute for operator review. |
| `provider` | Exact declared `ProviderConfig` fields, with local loopback endpoint, model name/digest, vision and structured output enabled, image/context limits. The served profile must match every declared field. No default checkpoint or guessed digest. |
| `deadlines_ns` | Exact job `scene`, `reset_final`, `reset_active` limits. Current bridge jobs freeze 5 s / 5 s / 3 s. |
| `image_budget`, `max_output_tokens` | Explicit limits within the model profile's capacity. |
| `observation_max_age_ns`, `validity_ns` | Explicit bounded evidence age and result validity. |
| `prompt_sha256`, `answer_schema_sha256` | Frozen generated prompt/template and output-schema hashes. |

The pure `freeze(mapping)` helper computes the last two hashes and validates explicitly supplied fields. It does not invent definitions, source approval, model identity or job identity. Canonical UTF-8 JSON uses sorted keys and compact separators. The resulting `FrozenSpec.sha256` binds every field. Retain the operator source and spec bytes with the new plan.

Run a **pure-file** check before any channel is enabled:

```bash
python -m levi.automatic.provider_worker check --spec /absolute/path/frozen-model-spec.json
```

`check` has no network/model SDK access and writes no files. Missing definitions, unconfirmed ISC, wrong source/prompt/schema hashes, missing model digest or incomplete fields block it. The bridge binding uses `providers.model_spec_file` and `providers.model_spec_sha256` together; it also checks the job's run, plan, ISC and all three deadlines. A missing spec path is blocked. The legacy external-file protocol remains readable, but its heartbeat is not proof that this new model worker exists.

The model profile must include every `ProviderConfig` field; omitted-field defaults cannot substitute for frozen configuration. Every field is checked against the actual shared Judge profile.

## Runtime worker and evidence

The shared online Judge must explicitly register the same frozen spec before a worker starts. The new `/v1/aeri/provider` route accepts its registered SHA, request, current JPEGs and cancellation identity; it accepts no caller prompt or replacement specification. `/v1/aeri/cancel` only cancels the matching request identity. Both inherit the loopback Host/Origin/body-limit checks.

An operator registers local frozen files through the existing live configuration:

```toml
[online]
aeri_specs = ["/absolute/path/frozen-model-spec.json"]
```

The list defaults to empty and is omitted when rendering a default configuration, preserving existing C5 configuration files. Entries must be distinct absolute paths to complete owned frozen specs. Configuration checking is file-only; endpoint construction loads the registered bytes without inference. An unknown SHA is unavailable. HTTP cannot register a spec. Changing this list or the job's binding requires retaining the new spec/plan identity; it does not start a service or certify its model profile.

A separately authorized worker uses:

```bash
python -m levi.automatic.provider_worker run \
  --spec /absolute/path/frozen-model-spec.json \
  --root /absolute/path/provider-files \
  --evidence-root /absolute/path/current-run/evidence \
  --online-url http://127.0.0.1:7882
```

The worker file root already exists and belongs to the executing identity. A new run evidence directory may be absent: bootstrap opens and checks only an existing owned ancestor without symlinks; it creates neither a run nor evidence. READY describes the model channel, never physical scene readiness. After execute creates the fresh run, the first ticket rechecks the complete authorized path. Replacement, symlinks or changed ownership block it. Evidence root is explicitly authorized and must match the request. The worker opens every directory component without following symlinks, reads owned regular request/JPEG files within byte limits, verifies frame hashes before and after inference, and replaces/fsyncs responses atomically. Images are temporary on the online Judge; the retained originals remain in the run's evidence store.

Requests live under `scene/requests` or `reset_judgement/requests`; their filenames are SHA256 of the request ID. Responses and cancellations use the same filename in their respective directories. The bridge supplies `run_id`, `plan_sha256`, `spec_sha256`, phase, monotonic `submitted_ns` and absolute `deadline_due_ns`. The deadline cannot exceed the frozen phase limit. Missing bindings, wrong identity/clock, old/future observations, changed or unretained hashes, incomplete predicates and downgraded required flags never grant readiness or reset confirmation.

The model returns only true/false/null for the complete frozen predicate set, current frame ref/hash citations, the spec/model identity and the captured observation time. Code computes `ready/reset_required/unknown` or `confirmed/rejected/unknown`. Optional predicates never decide. Every non-null reading needs current evidence; preferred views and minimum visible evidence also govern a reset confirmation. There is no invented calibrated confidence.

READY is published only after an actual live channel/profile check succeeds. It binds PID, process start ticks, boot ID, run/plan/spec/model and the two implemented capabilities. Stale, dead or reused process identity is refused on the model path. Channel/gate failures remove readiness. Stopping the worker removes READY. One directory lock prevents competing worker instances. Heartbeat does not mistake this worker's own legal in-flight request for external busy and remove existing READY. Its original ten-second age bound remains; no new readiness evidence is fabricated. A progressing poll cursor prevents the first thousand historical tickets from starving new ones, without deleting history.

## Cancellation and recovery

Whichever of C5/AERI runs first, a cancellation/timeout reply retains ownership, GPU admission and the mutex until the actual worker exits. AERI uses one serial GPU-check thread; supervision reads completed results, so a slow check cannot delay the frozen deadline or cancellation reply, and its unfinished thread retains ownership.

AERI calls and C5 use the same Judge mutex, model ownership, `online_admit/check/done` and transport abort. No private vLLM service is started and no GPU gate is bypassed. An external training owner continues to block admission under existing rules.

The actual request deadline is enforced outside the model Budget (whose minimum is 10 seconds). Timeout, file cancellation, gate closure or supervisor abort cuts the owned transport and makes the response unavailable. A late answer cannot succeed. The Judge keeps its mutex and GPU admission until the actual model thread exits, even when the caller already received an unavailable response; a model ignoring cancellation cannot overlap another C5/AERI call. If it never exits, the channel stays busy rather than silently granting a new call.

A durable intent prevents duplicate inference across restarts. A completed request reuses its original response only when request/spec/response hashes match. An interrupted intent produces unavailable on restart, not a guessed success or automatic inference replay. Changed content under the same request ID is refused. Keep request/response/processed files with the run evidence for audit.

With `allow_early_stop:false`, the current single reset policy still enters WAIT_HUMAN at reset horizon exhaustion, even with a confirmed `reset_active` candidate and a ready post-reset scene; it cannot autonomously continue forward. The current `_finish_reset` does not request `_final_judgement`, so this candidate is not a `reset_goal` final verification. This change preserves that gate. A bounded autonomous reset loop needs a separate design and acceptance.

## Verified limits

The tests exercise actual file worker, JPEG retention, AERI/ISC contracts, bridge request/collect paths and shared online Judge using injected models and fake HTTP. They include invalid JSON/spec/model/ref/hash/time/identity, missing or downgraded predicates, gate closure, cancellation, late models, restart ambiguity, no-network/no-write file check and explicit plan/deadline binding.

No real model inference, GPU latency measurement, robot SDK/network, physical parameters or onsite execution was performed for this change. Model quality and completing within 5/3 seconds remain unmeasured. Physical definitions, checkpoint/model digest, camera views, safety and onsite approval are operator gates. Do not report this mock verification as a robot-ready system.
