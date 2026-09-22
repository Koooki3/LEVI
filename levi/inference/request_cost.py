"""What one request costs a model, learned from the requests it has served.

A provider reports only a total per call. Every metered call LEVI makes adds
a sample (prompt characters, images, tokens) for that model; a least-squares
fit over the recent samples splits the total into a cost per character and
a cost per image. The harness sizes what it sends next with it -- how many
frames a refinement can carry -- so a local model with a small context is
filled, not overflowed, and the estimate sharpens with use. Until there is
enough to fit, ``calibrate`` measures it directly with two one-token calls.

Ollama may count only the prompt tokens it did not have cached, so samples
can under-state a prompt that shares a prefix with the call before it; the
margin and the context headroom the harness keeps absorb that.
"""

import numpy as np

from .gpu import _read, _state_dir, _write

# Keep this many recent samples per model.
SAMPLES = 60
# Over-estimate both costs by this much: a fit is not a promise.
MARGIN = 1.15


def _path(config):
    return _state_dir() / "request-cost" / f"{config.name}.json"


def _key(config):
    return f"{config.model}@{config.model_digest or ''}"


def record(config, chars, images, tokens):
    if not tokens or chars <= 0:
        return
    path = _path(config)
    value = _read(path, {})
    rows = value.get(_key(config), [])
    rows = [*rows, [int(chars), int(images), int(tokens)]][-SAMPLES:]
    _write(path, {**value, _key(config): rows})


def fitted(config):
    """(tokens per character, tokens per image), or None until the samples
    tell the two apart."""
    rows = _read(_path(config), {}).get(_key(config), [])
    if len(rows) < 2:
        return None
    data = np.array(rows, dtype=float)
    (per_char, per_image), _, rank, _ = np.linalg.lstsq(
        data[:, :2], data[:, 2], rcond=None
    )
    if rank < 2 or per_char <= 0 or per_image <= 0:
        return None
    return per_char * MARGIN, per_image * MARGIN


def calibrate(config, text, image_paths):
    """Two one-token requests -- the same kind of text without and with two
    of the run's own images -- give the fit its first samples.

    Returns (fit or None, tokens spent), so the caller can bill the run.
    """
    import base64
    import secrets

    from .gpu import require_free
    from .provider import client_for

    images = [
        base64.b64encode(path.read_bytes()).decode("ascii") for path in image_paths[:2]
    ]
    if len(images) < 2:
        return None, 0
    spent = 0
    require_free(config)
    client = client_for(config, timeout=300)
    for attached in ([], images):
        # A fresh prefix each time, so no cached prompt hides tokens.
        content = f"{secrets.token_hex(8)} {text}"
        message = {"role": "user", "content": content}
        if attached:
            message["images"] = attached
        response = client.chat(
            config.model,
            config.model_digest,
            [message],
            output_schema={"type": "object"},
            max_output_tokens=1,
            context_tokens=config.context_tokens,
        )
        usage = response["usage"]
        spent += usage["tokens"] or 0
        record(config, len(content), len(attached), usage.get("prompt_tokens"))
    return fitted(config), spent
