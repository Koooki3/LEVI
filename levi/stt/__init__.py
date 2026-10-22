"""Speech to text for the marvel0 page: one worker process, never in the core.

The core never imports Torch. A model runs in ``integrations/stt`` in its own
environment, started on the first request, tracked in ``levi/children.py`` and
gone after two minutes without one. Audio lives in memory and in a pipe only; a
transcript is returned to the page that asked and written nowhere (not to a
log, a file or an error message). See docs/SPEECH.md.
"""
