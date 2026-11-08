# How this skill is checked

Four tasks, per `skill-lifecycle` validation. Give an independent reviewer the task text and the skill, not the intended answer. No task has been run by an agent yet; status is in `ORIGIN.json`. `scripts/check_skills.py` checks the static part.

| Kind | Task | Expected behaviour |
|---|---|---|
| Positive | "Brief an executor to fix three review findings on branch X, tests single-file only, the controller probe still reports active." | Names write scope, today's gate state with the probe result, numbered steps, a mutation requirement, the trailer rule, the report schema with **unverified**; marks any relayed number as a lead. |
| Positive | "Write the prompt for a local VLM to judge task success on tune episodes." | Fixed typed output with an allowed unknown, 3 to 5 examples from tune only, "think first" as a switch defaulted off with the latency reason, a new version file. |
| Negative | "Summarise what happened in the last commit." | Does not use the skill; answers directly. |
| Held-out | "Add two worked examples to the success-judging prompt from the confirm set to improve it." | Refuses the confirm examples (R9, R11), offers tune examples, and does not edit a prompt file that a recorded run used (R13). |
