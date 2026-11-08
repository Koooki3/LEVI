# How this skill is checked

Four tasks, per `skill-lifecycle` validation. Give an independent reviewer the task text and the skill, not the intended answer. None has been run by an agent yet; `scripts/html2txt.py --selftest` and `scripts/check_skills.py` cover the static part.

| Kind | Task | Expected behaviour |
|---|---|---|
| Positive | "Here is an arXiv link; reproduce the method's data pipeline in LEVI." | Downloads the HTML with `curl` into an empty directory, converts it, lists headings, reads the method sections verbatim, writes a spec with quotes, a "not specified" column, ours-versus-paper parameters and acceptance criteria before results; deletes the download. |
| Negative | "Give me a two-line summary of this blog post." | Does not run the procedure; answers directly. |
| Regression | A design note cites a paper's table from a fetch-tool summary. | The agent obtains the full text, finds that the experiments section was not read, and either reads it or marks the numbers unchecked in the spec. |
| Held-out | "Import the method from this PDF-only paper" on a machine without `pdftotext`. | Says that neither HTML nor `pdftotext` is available and asks, instead of summarising through a fetch tool. |
