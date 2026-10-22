"""GitHub Actions error annotations for failed pytest tests.

The run's logs, step summary and JUnit artifact all need a signed-in account,
but annotations are public (``GET /repos/{owner}/{repo}/check-runs/{id}/annotations``
answers anonymously). ``tests/conftest.py`` prints these lines at the end of a
pytest session when ``GITHUB_ACTIONS=true``, so the names of the failed tests
can be read without anyone downloading the artifact. GitHub keeps at most 10
error annotations per step: the first line lists every failure, the next ones
name single tests with the first line of their message.
"""

from __future__ import annotations

MAX_ANNOTATIONS = 10
MAX_LIST = 60
MAX_MESSAGE = 300


def _data(text: str) -> str:
    """Escape a workflow-command message (percent, CR and LF)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _prop(text: str) -> str:
    """Escape a workflow-command property value (also ``:`` and ``,``)."""
    return _data(text).replace(":", "%3A").replace(",", "%2C")


def lines(failures: list[tuple[str, str, str]]) -> list[str]:
    """``::error`` lines for ``(kind, nodeid, message)`` triples, kind = failed or error."""
    if not failures:
        return []
    names = [f"{kind} {nodeid}" for kind, nodeid, _ in failures]
    shown = names[:MAX_LIST]
    if len(names) > MAX_LIST:
        shown.append(f"... and {len(names) - MAX_LIST} more")
    out = [
        f"::error title={_prop(f'pytest: {len(failures)} failed or errored')}::"
        + _data("\n".join(shown))
    ]
    for kind, nodeid, message in failures[: MAX_ANNOTATIONS - 1]:
        first = message.strip().splitlines()[0] if message.strip() else ""
        path = nodeid.split("::", 1)[0]
        out.append(
            f"::error file={_prop(path)},title={_prop(f'{kind}: {nodeid}'[:200])}::"
            + _data(first[:MAX_MESSAGE])
        )
    return out
