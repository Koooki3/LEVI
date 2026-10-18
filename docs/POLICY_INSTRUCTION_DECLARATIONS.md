# Policy instruction declarations: provenance and read-only comparison

[中文](POLICY_INSTRUCTION_DECLARATIONS.zh-CN.md)

Status: design only; task `T-C-POLICY-DECLARATION-DESIGN`, based on `020007fe`.
No producer, verifier, command, registry extension, runtime guard or real policy declaration is implemented by this document. MODELREG stays paused. No tests or robot/model operations were run for this design.

## Problem and existing boundary

The subtask planner validates objects and renders policy text; it does not establish training coverage. `levi/automatic/subtask_loop.py` owns `INSTRUCTION_TEMPLATES`, `render`, `parse_plan` and `Subtask.instruction`. The model's `suggested` text is not the dispatched instruction. A template mismatch is a reason to investigate, not evidence of incapability.

`levi/domain/aeri.py::JobDeployment` freezes config, checkpoint ID and directory. `Checkpoint.sha256` is optional. `levi/automatic/fr3_bridge.py::check_identity` compares config and checkpoint location, not verified weight content. `levi/automatic/cli.py` freezes only the forward deployment for subtask mode: reverse uses that same policy, not an absent reset deployment.

The existing hub design `programs/levi2/MODEL-REGISTRY-AND-RUNRECORD-DESIGN.md` proposes a registry reference layer; its recorded first version leaves `sha.verified` null. A directory, fingerprint, declared checksum or `registry_sha` is not authenticated proof of weight identity. `programs/levi2/NATIVE-HARVEST-20261018.md`, “后续设计审查与开发范围”, keeps this work at design until producer, digest specification, provenance and consumer are available. These hub references identify internal design sources, not product dependencies.

## Responsibilities and trust

| Role | Future responsibility | Current availability |
| --- | --- | --- |
| Artifact owner/exporter | Issue a complete immutable artifact inventory and effective inference configuration for the delivered policy | No approved producer for this contract |
| Training owner | Export exact instruction strings with stable source entry IDs, source revision and linkage to that artifact | No reviewed declaration supplied |
| Authorized verifier | Verify artifact bytes and dependency closure, validate source authority and instruction provenance; issue a bounded attestation | No approved verifier or trust-root policy for this contract |
| Existing registration layer | Preserve references to accepted evidence and verification status; never turn a declared checksum into verified | Existing design/first implementation only; do not resume it here |
| Comparison consumer | Read accepted immutable inputs and compare actual rendered text without granting execution | Proposed below; not implemented |

An owner statement and a training-manifest extract are different provenance classes. Neither proves task success, reachability or safety. Trial success/failure cannot generate declaration entries. Do not inspect training data, frozen evaluation material or user configuration to fill missing declarations automatically.

A hash proves content equality, not who issued it. An attestation needs an independently established issuer identity and integrity protection: a signature checked against an already approved trust root, or an explicitly approved authenticated publication/review mechanism with immutable artifact references. A self-signed key embedded in the declaration, a `verified=true` field, author name, Git commit ID or registry checksum alone is insufficient. Trust-root selection, revocation and authenticated publication are unresolved prerequisites; this design creates no keys or signing service. Without them the consumer reports `unknown`, even if checksums match. “Unforgeable” is not a claim made by this document: authenticity is conditional on the approved mechanism and its threat model.

## Proposed versioned evidence contracts

Names below are design proposals, not existing registered schemas.

- Artifact manifest `levi.policy_artifact_manifest.v1`: artifact ID, format, complete file inventory (`relative_path`, `size_bytes`, `sha256`) and explicit referenced dependency manifests. Include every weight shard, model configuration, tokenizer/vocabulary if used, normalization statistics, and input/output transform assets that affect inference. An unexplained missing dependency makes verification incomplete.
- Effective configuration `levi.policy_inference_config.v1`: serving implementation revision and dependency/environment lock digest; resolved model config, normalization and transform references, action layout/horizon, and behavior-affecting inference settings. Include effective defaults, not only CLI overrides. Exclude ports, log paths, timestamps and secrets. If the serving implementation cannot enumerate an effective setting, mark coverage incomplete rather than guess.
- Declaration `levi.policy_instruction_declaration.v1`: declaration ID/revision; artifact-manifest digest; effective-config digest; provenance class (`training_manifest` or `owner_declaration`); immutable source ID/revision/digest; issuer reference; instruction entries (`exact_text`, `source_entry_id`). The entry states only that this text was declared for this identity. No implicit completeness claim or denylist.
- Verification attestation `levi.policy_declaration_attestation.v1`: exact digests of all above inputs, verifier implementation/revision, authenticated issuer reference, verification procedure/evidence references, coverage limitations, verified-at and validity/revocation policy reference. Verification must cover instruction-to-artifact linkage as well as file integrity. The declaration producer cannot certify itself merely by adding this object.

A future binding is the pair of verified artifact-manifest and effective-config digests, plus config/checkpoint IDs for diagnostics. Display names and directories are not identity. A supplied binding must be checked against the frozen run policy; accepting an arbitrary matching declaration/attestation pair is insufficient.

### Proposed digest specification

For each JSON contract, hash its canonical payload bytes, excluding its detached signature and external digest field. Reject duplicate keys, unknown fields, non-finite numbers and schema-invalid values before canonicalization. Proposed encoding: RFC 8785 canonical JSON, UTF-8, no BOM or trailing newline; versioned domain prefix `schema_id + "\n"` precedes the payload. Implementation and cross-language reference vectors require approval before use; no digest in this document is claimed to follow an existing LEVI standard.

Manifest paths are relative POSIX paths: reject absolute paths, empty components, `.`/`..`, backslashes and duplicate paths; sort entries by their exact UTF-8 path bytes. Reject symlinks in the first version rather than following them. Weights are hashed as original file bytes. References to external/base assets must resolve through separately verified immutable manifests within authorized roots; absent or mutable references yield incomplete verification. Actual artifact verification is a separately authorized resource-coordinated job, never a comparison-time scan.

Instruction text is preserved exactly: no case folding, Unicode normalization, trimming, whitespace collapsing, synonym matching or color removal. JSON escaping is canonicalized, the decoded string is not rewritten. Source exports that changed spelling must retain an explicit provenance transformation and cannot be represented as exact training text. A digest-spec change requires a new schema version and new attestations; do not silently reinterpret old hashes.

## Actual proposed consumer and outputs

The first consumer is an offline, explicitly invoked report in the existing AERI check/analysis workflow, not an unused generic library. It compares a recorded plan's successfully parsed subtasks with the frozen forward deployment and accepted registration evidence. Suggested placement is a future read-only subcommand beside AERI check/analysis, with explicit run/plan and declaration references. CLI name and registration integration remain unresolved; do not advertise a callable command.

A pre-run job check cannot inspect a VLM plan that has not been generated. It may report only declaration availability and binding status. Text comparison first becomes possible on a saved plan; adding it to live planning is a separate reviewed change. The current first consumer is therefore a human-readable offline report, not a live dispatch gate.

For historical plans, preserve the original recorded dispatched instruction and its run/episode/journal provenance, plus the parser/template revision or frozen template digest. Re-parsing model JSON with current `INSTRUCTION_TEMPLATES` can produce different text: never label that reconstruction as the historical actual instruction. Verify recorded text against the frozen original renderer when available; missing original provenance or renderer version yields unknown. A current re-render may be shown separately as a hypothetical comparison, never substituted for the original.

The future pure comparison accepts validated `Subtask` instances, frozen policy identity, an accepted verification result from the registration verifier, and immutable declaration evidence. It does not accept a caller's boolean as proof of authentication. It performs no filesystem/network access and no model call. The trusted adapter obtaining that verification result is a required part of the consumer, not an optional afterthought.

Per-step fields: index, actual `instruction`, `match: declared|unknown`, `reason_code`, matching source entry ID and evidence digests. Report fields: binding status (`matched|missing|unverified|mismatch|revoked`), parser outcome, declaration revision/digest, provenance class, source references, and `execution_authorized: false` without an override. `declared` means exact membership in an authenticated declaration for the verified frozen identity; it never means trained successfully, supported, safe or executable.

Missing declaration, missing or incomplete verification, unsupported schema, expired/revoked evidence, digest mismatch and unlisted text yield unknown with a diagnostic; malformed input yields an invalid-input report, not a model capability judgement. Return no positive membership results when the policy binding fails. Never rewrite a plan, change retry limits or pick a replacement target.

Only a successfully parsed reverse empty plan may produce `no_steps`; this is no assertion of restored scene or capability. Forward empty-plan refusal, malformed JSON and parser failures remain parse errors, never `no_steps`. Reverse always uses the frozen forward policy identity in subtask mode. The report cannot produce actions, labels, reset success or resume commands.

## Acceptance matrix and implementation gate

| Case | Required future result |
| --- | --- |
| Exact rendered text, verified frozen identity and authentic current evidence | declared with provenance; execution_authorized remains false |
| Only model-suggested text matches | unknown for the actual rendered text |
| Changed verb/color/article/whitespace/Unicode spelling | unknown; no rewrite |
| Missing producer, verifier, trust root or source linkage | unknown; missing prerequisite named |
| Same directory, changed shard/normalization/transform/config | binding mismatch or unverified, no declared entries |
| Supplied identity differs from frozen run | mismatch, even if declaration and attestation agree with each other |
| Self-asserted verified flag or self-signed embedded key | not accepted as verification |
| Duplicate fields/files/instructions, escaping ambiguity or symlink | invalid input; no scan or traversal |
| Empty reverse plan after successful parse | no_steps, not restored |
| Empty forward plan or malformed plan | original parse refusal preserved |
| Reverse subtask with only forward deployment | use forward identity; never fabricate reset identity |
| Unknown schema/digest version, expired/revoked attestation | unknown; no migration by inference |
| Owner statement instead of training export | provenance stays owner_declaration, never upgraded |
| Repeated comparison | deterministic output, unchanged inputs; no I/O, labels or control effects |

Before implementation: approve producer/verifier ownership and authentication mechanism; settle digest coverage and reference vectors with MODELREG owners without resuming their task; obtain an explicitly authorized sample declaration and verified artifact binding; fix the offline report's caller contract and maintenance owner; independently review synthetic acceptance cases. No real artifact or training access is authorized by this design. Later runtime integration and real-robot validation need their own review/authorization.

The benefit is traceable identification of undocumented instruction text. The cost is maintaining attestations across artifact/config/template changes, preserving provenance and revocation state, and verifying dependency coverage. Until the missing producers and consumer adapter exist, retain this design rather than creating a second registry or a module that accepts invented verification. Existing reverse-capability and template-phrasing issues remain open; paired wording evaluation is not replaced by declaration matching.
