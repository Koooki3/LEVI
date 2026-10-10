# Skill Loom for coding agents

[中文](SKILL_LOOM.zh-CN.md)

[Skill Loom](https://github.com/Koooki3/skill-loom) provides evidence-based,
reversible maintenance of the skills used by Codex, Claude Code and other coding
agents working on LEVI. LEVI includes its `skill-lifecycle` instructions and
references at version **0.1.1**, commit
`34efa6e539e2d1da829d05652d6c0df1c1433a7a`. The upstream instructions are unchanged;
`LICENSE` and `ORIGIN.json` preserve the MIT license, source and file hashes.

## Skill discovery

Start the coding agent in this LEVI checkout, or a subdirectory belonging to this
Git repository. A separate nested Git repository has its own discovery boundary.

| Host                     | Project entry                                 | Explicit request                     |
| ------------------------ | --------------------------------------------- | ------------------------------------ |
| Codex                    | `.agents/skills/skill-lifecycle`              | `$skill-lifecycle`                   |
| Claude Code              | `.claude/skills/skill-lifecycle/`             | `/skill-lifecycle`                   |
| Other Agent Skills hosts | Configure the host to read one of these roots | Use the host's own invocation syntax |

The Codex entry is a relative directory symlink to the physical Claude Code
entry, so both hosts read one copy. Keep Git symlink support enabled; a checkout
that materializes the link as a text file needs a host-supported directory link
before Codex can discover the skill. Ask the host to report the loaded
`SKILL.md` path; use a new session if it has not picked up the new entry.
Discovery follows the [Codex skill documentation](https://learn.chatgpt.com/docs/build-skills)
and [Claude Code skill documentation](https://code.claude.com/docs/en/skills).

Example request:

> Use skill-lifecycle to inspect LEVI's project skills. Explain the findings and
> proposed changes, then wait for approval before writing any files.

## Optional local CLI

The skill can follow the maintenance workflow using available file tools.
Install the Python CLI when its inventory, plan/apply and rollback commands are
needed. It uses Python 3.10+ and uv, with an environment dedicated to Skill Loom.
Run the following setup only after approval, from the main LEVI checkout, with
the destination directories absent:

```bash
git clone --no-checkout https://github.com/Koooki3/skill-loom.git lab/tools/skill-loom
git -C lab/tools/skill-loom checkout --detach 34efa6e539e2d1da829d05652d6c0df1c1433a7a
uv venv --python python3 lab/tools/skill-loom/.venv
uv pip install --python lab/tools/skill-loom/.venv/bin/python --editable ./lab/tools/skill-loom 'PyYAML==6.0.3'
mkdir -p lab/skill-loom/checks lab/skill-loom/candidates lab/skill-loom/journal
cp lab/tools/skill-loom/examples/user-profile.json lab/skill-loom/profile.json
```

Edit the private profile to describe the user's needs, language, observation
scope and budgets. The example's weights and budgets are starting values, not
measured quality thresholds. Keep one repair round initially. The maintainer's
existing profile uses Chinese, explicit approval before writes, and observation
of selected project skill directories only. `lab/` stays outside Git.

In the main checkout or a feature worktree, resolve the shared tool and state
locations without changing LEVI's product environment:

```bash
loom_git_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
loom_checkout="$(dirname "$loom_git_dir")"
loom_python="$loom_checkout/lab/tools/skill-loom/.venv/bin/python"
loom_state="$loom_checkout/lab/skill-loom"
"$loom_python" -m skillloom --version
"$loom_python" -m skillloom --help
```

The following commands inspect the profile and physical project skill root, and
write reports to the private state directory. Report writes also need approval.
Choose a new run directory for each check so previous evidence remains intact:

```bash
loom_run="$loom_state/checks/run-001"
mkdir "$loom_run"
"$loom_python" -m skillloom doctor --profile "$loom_state/profile.json" --out "$loom_run/doctor.json"
"$loom_python" -m skillloom inventory --root "$PWD/.claude/skills" --out "$loom_run/inventory.json"
```

Inventory only one of the two discovery roots at a time: scanning both reports
the shared skill twice. `doctor` checks profile validity and executable presence;
`inventory` checks metadata, local links and script syntax. Verify host discovery
and actual skill invocation separately.

## Reviewed changes and rollback

Prepare a single candidate under a new private directory with the same basename
as the target skill. Review its fixed source commit, license, dependencies,
scripts, triggers and changes to the local customization. Run task-appropriate
checks, then create the exact change plan. Use the physical root in the feature
worktree you own:

```bash
"$loom_python" -m skillloom plan \
  --candidate "$loom_state/candidates/run-001/skill-lifecycle" \
  --root "$PWD/.claude/skills" \
  --journal "$loom_state/journal/run-001" \
  --out "$loom_run/plan.json"
```

Read the plan file and retain the printed `review_digest`. After approval of the
specific changes, pass that exact value to `apply`:

```bash
"$loom_python" -m skillloom apply --plan "$loom_run/plan.json" --digest '<reviewed-digest>'
"$loom_python" -m skillloom rollback --transaction '<transaction-directory-returned-by-apply>'
```

Rollback also needs approval. Candidate, skill root and journal must be disjoint.
Skill Loom rejects changes to a linked target or root; leave `.agents/skills`
discovery links in place. If the candidate or installed files drift, review a new
plan. A digest binds the plan contents and cannot grant permission.

Transactions retain absolute target paths. Roll back while the owning worktree
still exists; its transaction cannot undo files merged into the main checkout.
After review and Git integration, keep the transaction as development evidence
and use a new reviewed Git change to undo the integrated configuration. Future
skill updates each need their own plan and journal.

Follow [AGENTS.md](../AGENTS.md) and the maintainer's workspace manual for approval,
file ownership, independent review and driver-only integration. Maintenance scope
is limited to approved project development skills and private Skill Loom state.
User homes, credentials, host sessions, managed plugins, source datasets and gold
remain outside that scope. Enable schedules or hooks only through a separately
approved configuration change.

## Verification

Use an isolated offline fixture to test install, update, rollback and no-change
behavior. These commands write only to the named test directory and temporary
test files; obtain approval first and remove the fixture after retaining results:

```bash
"$loom_python" -m skillloom demo --workdir "$loom_run/demo"
(cd "$loom_checkout/lab/tools/skill-loom" && "$loom_python" -m unittest discover -s tests -v)
```

Check that each host discovers the intended entry, invokes it for a maintenance
request and leaves ordinary coding tasks outside its trigger. CLI tests and
static discovery checks do not establish model-task quality. The local setup
records checks under `lab/skill-loom/checks/`; a host without an available
executable remains unverified until tested there.
