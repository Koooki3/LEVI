# Codex / Claude Code Pilot

Status: experimental. Linux, WSL and VS Code Remote/SSH are the first supported
hosts. “Offline” means **without the LEVI Web UI**, not without model networking.
API-driven annotation remains an online Workbench channel. All channels share
LEVI's capability registry, executable plans, evidence, validators and commits.
No real-model quality or GPU acceptance is implied by protocol tests.

## Install in order

From the LEVI checkout, using the same workspace for every command:

```bash
uv sync --locked --extra agent
bun install --frozen-lockfile
# Optional: needed only when LEVI starts a managed Codex/Claude session.
# Node.js >=22 must also be available on PATH.
(cd integrations/pilot && bun install --frozen-lockfile)
bun run build
```

`LEVI_WORKSPACE` defaults to this checkout's `.state/`. Set it explicitly before
starting commands if your datasets/workbench live elsewhere. Do not copy old
absolute checkpoint/worker paths into a new machine. Configure those using the
[SAM3 deployment guide](SAM3.md). Pilot setup never downloads a checkpoint.

The optional lock pins `@agentclientprotocol/codex-acp` 1.12.0 and
`@agentclientprotocol/claude-agent-acp` 0.79.0, including their runtime dependencies.
This uses their packaged runtime, not an arbitrary already-running IDE session.
Both reuse the official local authentication location. LEVI stores no copy of
account tokens. If authentication cannot be determined, its status is **unknown**;
installation is not proof of login. Log in with your official client:

```bash
codex login
# Or, for Claude Code:
claude auth login
```

Provider access/subscription support depends on the installed official runtime.
LEVI does not convert a subscription into an API key or silently switch billing
providers. Account/configuration changes detected between planning and execution
require a new approved plan. Token refresh alone is not treated as an account
switch. Authentication using an OS keychain may not expose a stable account ID;
use pause/disconnect before switching such accounts.

## Existing CLI or VS Code session

First register/open your dataset using LEVI's normal catalog tools; use its
`local/<catalog-name>` ID. The two built-in demo IDs are also accepted. Review a
project-local configuration diff, then explicitly apply it:

```bash
uv run levi agent connect --client codex --project . --dataset local/my_dataset
uv run levi agent connect --client codex --project . --dataset local/my_dataset --apply
# Claude Code uses the same sequence with --client claude.
```

This configures `.codex/config.toml` or `.mcp.json`, preserving other entries,
and adds the LEVI overview skill if absent. It does not edit global settings.
An existing `levi` entry is not overwritten: revoke its connection, remove only
that project entry, and reconnect. Reload MCP in the official client; project
trust/enablement is controlled by that client. A migrated checkout needs a fresh
connection because executable/workspace paths and grants are machine-local.

Connection credentials live in a mode-0600 file under the LEVI state directory;
project files contain its path, not the token. Grants expire (24 hours by default,
`--hours` to change), are dataset scoped and have a tool-call cap. Disconnect with
`uv run levi agent disconnect <connection-id>`, or the Workbench connection card.

Ask Codex/Claude to discover LEVI tools, inspect your dataset and create a plan
with `provider: "external"`. MCP tool names use `__`, for example
`runs__plan`, `runs__prepare`, `media__sample`, `annotations__propose_segments`,
`view__seek`, `runs__finish`, and `runs__result`. Legacy dot-name calls remain
accepted. Tool schemas are the source of truth. Skills are also MCP resources;
clients need not support automatic skill discovery.

The MCP subprocess starts Core Host on demand. It does not start Next.js or a
browser. In VS Code Remote/SSH or WSL, run both LEVI and the tool process on the
data host; forward the UI port only when you want the browser.

## Human-reviewed headless workflow

External agents cannot approve execution, accept a pilot, approve a ChangeSet
or commit it. A human runs these commands in an interactive terminal. Approval
prints the exact revision and requires typing `approve`; piped confirmation is
rejected. This is an application authority boundary, **not an OS sandbox against
an unrestricted agent running as the same user**.

1. Have the external agent call `runs__plan`, or create a JSON file yourself:

```json
{
  "provider": "external",
  "pilot_runtime": "codex",
  "repo_id": "local/my_dataset",
  "episodes": [0],
  "cameras": ["observation.images.front"],
  "instruction": "Review this episode and flag uncertain or failed actions with evidence.",
  "allow_media_egress": true,
  "workflow": {"kind": "review"},
  "budget": {"max_calls": 8, "max_tokens": 16000, "max_seconds": 300}
}
```

Use actual catalog IDs/camera keys. Media consent covers the selected evidence
sent through your runtime. For temporal definitions or object targets, follow
[Agents](AGENTS.md); guessing a camera or omitting definitions is not a
substitute for clarification. Use `pilot_runtime: "claude"` for managed Claude.

```bash
uv run levi agent plan create plan.json
uv run levi agent plan show <run-id>
uv run levi agent plan approve <run-id>
```

2. In an existing client, ask the agent to prepare evidence and produce the
   approved pilot draft. For a managed session, start it instead:

```bash
uv run levi agent pilot start <run-id> --runtime codex
uv run levi agent pilot status
uv run levi agent events <run-id>
uv run levi agent permission list
# If a permitted runtime tool requires a separate human decision:
uv run levi agent permission answer <permission-id> --option <option-id>
```

Managed sessions have no LEVI terminal/filesystem RPCs. Claude's adapter is
configured with no built-in tools and no discovered project/user settings;
Codex has shell/unified-exec disabled, read-only mode and web search disabled.
Unexpected terminal/file permission requests are rejected. Runtime-specific
controls are not advertised as OS isolation; existing external clients retain
their own host permissions. The session's LEVI grant cannot approve or commit.

3. Inspect and correct the persisted draft, then accept or reject pilot quality:

```bash
uv run levi agent changes show <changeset-id>
# Optional: proposals.json contains the complete edited proposal array.
uv run levi agent changes edit <changeset-id> --file proposals.json
uv run levi agent changes validate <changeset-id>
uv run levi agent pilot review <run-id> --text "Checked evidence and boundaries"
# Or add --reject and state what needs correction.
```

Mask review should use the existing player/sidecar tools. The terminal does not
pretend an RLE dump is a visual quality inspection; pending object review blocks
approval. After pilot acceptance, ask the existing agent to continue, or send a
managed-session message:

```bash
uv run levi agent pilot message <session-id> --text "Continue only the remaining approved scope"
```

4. After validation and human review, publish and export separately:

```bash
uv run levi agent changes review <changeset-id>
uv run levi agent changes commit <changeset-id>
uv run levi agent changes export <changeset-id>
uv run levi agent result <run-id>
```

Commit uses a stable revision-specific idempotency key. Export reuses the
existing native exporter and verifies the resulting sidecars/provenance. It
exports the full dataset, preserving unselected episodes unchanged; it does not
silently claim to be a filtered export.

## Online Workbench and recovery

```bash
uv run levi
# Or start the UI and select a saved run:
uv run levi agent open <run-id>
```

The ordinary launcher attaches to the same core and database. Select the API /
external MCP, Codex Pilot or Claude Code Pilot execution channel. Approve the
plan and start Pilot from the existing Dock. Connection cards show the installed
adapter, known/unknown login state, official account commands and grant revocation.
“Disconnect LEVI” does not log out of Codex/Claude; an official logout can affect
other CLI/IDE sessions and should be performed deliberately.

The event panel distinguishes core-confirmed LEVI actions from runtime messages.
It uses sequenced SSE plus paged replay on disconnection. Existing terminal/IDE
sessions expose LEVI calls and products, not their unrelated conversation or
private reasoning. Managed sessions additionally expose public runtime events.
Large public events are retained in bounded, redacted JSONL artifacts; no global
transcript scraping or cloud relay is used.

Evidence seeks use the existing player. Follow is opt-in, stays in the current
dataset/episode, and pauses for unsaved draft edits. Other episodes must be
opened after saving. The manifest lists real persisted files, not temporary
mock overlays; object review still reads the native sidecar.

```bash
uv run levi agent pilot pause <session-id>
uv run levi agent pilot resume <session-id>
uv run levi agent pilot cancel <session-id>
uv run levi agent core status
uv run levi agent core stop
```

Pause/cancel revokes new LEVI tool calls before interrupting the runtime.
Requested stop and actual disconnection are separate states. Resume creates a
new runtime conversation using durable LEVI task state, retained evidence and
drafts; native transcript continuation is not promised. Cumulative managed
turn/time limits survive reconnection. A cancelled task cannot be restarted.
Unknown runtime usage is never shown as zero or as a guaranteed billing limit.

Closing the browser or frontend leaves Core Host running. Explicit core stop
terminates its owned runtime children and workers. Restart marks old sessions
interrupted and revokes their grants. No task is silently auto-resumed. Core
uses a private Unix socket and an authenticated loopback HTTP compatibility
listener (default 7861); it refuses occupied ports rather than killing another
service. Very long workspace paths may exceed the system Unix socket limit.

## Products, migration and verification

Below the configured workspace:

- `outputs/LEVI/workbench/agent/core/`: private socket, instance marker and control key.
- `outputs/LEVI/workbench/agent/connections/<timestamp>/`: revocable machine-local credentials.
- `outputs/LEVI/workbench/agent/datasets/<dataset-name>/runs/<timestamp>/`: evidence, object drafts, public runtime events, `result.json` and `result.md`.
- `outputs/LEVI/exports/<dataset-name>/…`: existing reviewed exports.

No hash suffixes are introduced. Manifest paths resolve relative to its
`path_base` under the workspace. Source datasets are never moved or rewritten.
Do not publish runtime directories, credentials or local project configuration.

Automated acceptance uses fake model/protocol processes, CPU fixtures and a
mocked browser: approval bypass, grant expiry/revocation, interleaved ACP events,
permission refusal, action pairing, manifest readability, configuration
preservation, core attachment, cancellation, and existing annotation/export
regression. Real CLI/IDE accounts, model quality, media interpretation and
provider billing remain a separate, explicitly authorized acceptance step.

## Sources and compatibility

- [Codex App Server](https://developers.openai.com/codex/app-server) and [MCP](https://developers.openai.com/codex/mcp).
- [Codex ACP](https://github.com/agentclientprotocol/codex-acp) and [Claude ACP](https://github.com/agentclientprotocol/claude-agent-acp): Apache-2.0 adapter packages, locked optional dependencies.
- [ACP protocol](https://agentclientprotocol.com/protocol/v1/overview): initialization, sessions, permissions and updates.
- [Happy](https://github.com/slopus/happy): session-follow/control interaction reference; no code or cloud service imported.
- [OpenCode](https://github.com/anomalyco/opencode): client/server separation reference; no second executor imported.

Pinned-adapter source was inspected for the implemented options. A real managed
session has not been run during automated validation. Capability negotiation and
clear errors replace claims of compatibility with every installed CLI version.

Managed session duration is reserved before execution and accumulated across
resumes. After an abrupt core crash, an unreconciled reservation remains charged
conservatively; revise and approve the plan budget before continuing if exhausted.
