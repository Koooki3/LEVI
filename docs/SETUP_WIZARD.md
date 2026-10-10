# Setup wizard: recipes and status probes

English | [中文](SETUP_WIZARD.zh-CN.md)

LEVI can guide a person through setting up a real-robot session: which
command comes next, what it touches, and whether the step it starts is
already healthy. This page covers the two parts that exist so far: the
**recipe registry**, which ties each step to the machine's own operator
guide, and the **read-only status probes** behind `GET /api/levi/setup/status`.
Neither starts, stops or connects to anything.

## Recipes

A recipe is one excerpt of the machine's operator guide (a Markdown file)
plus what it touches and how an interface may offer it. The recipe file holds
the machine's own paths, addresses and serial numbers, so it lives outside the
repository; point `LEVI_SETUP_RECIPES` at it and `LEVI_SETUP_DOC` at the guide.

```toml
version = 1

[[recipe]]
id = "R-CHK-3"
title = "GPU, process and port snapshot"
risk = 1            # 1 read-only, 2 LEVI/vLLM/recorder service, 3 robot stack or policy server, 4 makes the robot move
ui = "native"       # native | execute | copy | link
requires = []      # ids of recipes that come first
preconditions = ["nothing else is needed"]

[recipe.source]
section = "1"       # the guide's section number
heading = "Order"   # its title, when recorded (a renamed heading is drift)
block = 1           # code block of that section, from 1; absent = the whole section body
pick = [4, 6]       # optional line range inside the block
lines = [58, 60]    # where it was when recorded (reference only)
sha256 = "<64 hex digits of the normalised excerpt>"
# text = "..."      # optional: the recorded excerpt, to show a difference on drift

[recipe.touches]
robot = false
gpu = false
moves = false
commands_robot = false
listens = []        # ports it opens
connects = []       # ports it connects to
```

**Safety rules (the validator refuses a recipe that breaks one, and leaves it
out):**

- Class 3 and class 4 recipes are never `execute` and never `native`: they
  are only offered as `copy` or `link`.
- The one exception is a class 3 policy server (`kind = "policy_server"`) that
  only loads a model: it may be `execute` if `touches.robot`, `moves` and
  `commands_robot` are all false and it carries
  `confirm = { required = true, decision = "<who allowed it, when>" }`.
  Nothing in LEVI runs it yet.
- `moves = true` requires class 4; `commands_robot = true` requires class 3
  or 4.
- A recipe that connects to a robot-side port (5000, 5001, 5100, 7470, 8000)
  is only `copy` or `link`: LEVI never connects there.

**Drift.** The hash covers the excerpt after Unicode NFC, trailing spaces
removed and leading and trailing blank lines removed; any other change, one
character included, is drift. A check finds each excerpt by section number
and block index, never by line number:

| Status | Meaning |
| --- | --- |
| `ok` | the excerpt is unchanged (`shifted` when it now sits on other lines) |
| `drift` | the text or the section heading changed: review it before anyone copies the command (a unified diff is shown when the recipe records `text`) |
| `moved` | the same text is elsewhere (blocks reordered, section renumbered): update the recipe's source |
| `missing` | the section, the block or the picked lines are gone |
| `ambiguous` | the section number appears more than once |
| `doc_error` | the guide cannot be read reliably (a code fence left open) |

```bash
levi setup recipes check --recipes site/setup-recipes.toml --doc setup.md    # exit 0 / 1 (problems or drift) / 2 (unreadable)
levi setup recipes check --json
levi setup recipes excerpt --doc setup.md --section 1 --block 1 --pick 4 6  # hash and lines, to write a recipe
```

Both commands only read the two files. Updating the recipe file after drift
is a person's decision (the guide is maintained separately).
