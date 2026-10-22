"""The refusals of the speech worker's manager.

``code`` is a stable name (the HTTP layer prefixes it with ``speech_`` or maps
it to a status of its own), ``message`` an English sentence. Neither ever holds
a transcript, a worker message or a path.
"""


class SttError(Exception):
    """One refused or failed transcription, or worker start."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.extra = extra
