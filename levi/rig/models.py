"""The validated model catalog of the policy deploy panel.

``levi.automatic.policies.discover_report`` lists the checkpoint folders (read
only: it opens no weights and follows no link); this module keeps the ones the
panel may deploy and says why it left out each other one. A checkpoint is
offered only when it is a JAX checkpoint with ``params/`` that
``discover_report`` calls deployable, its role is ``forward``, its serving
config is one the site file knows, the site does not exclude it, its default
rollout group (``<id>_rtc_demo``) fits the 80 characters the client command form
accepts, and a **top-level** ``norm_stats.json`` is a regular file (``discover_report`` also
accepts one under ``assets/``, but the policy server only reads the top-level
file shipped with the checkpoint, ``serve_policy.py``). Only the site file
decides which configs take ``--cfg-guidance-scale``, which ids are restricted
to the client's known task instructions, and which have a recorded real-GPU
serving check (every other option carries the label ``unverified``).

The answer is cached for 30 seconds per site; nothing here names a path.
"""

import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from levi.automatic import policies

from . import site as rig_site

SCHEMA = "levi.rig.models.v1"
# The family the demo client restricts to its two known task instructions
# (RESTRICTED_MODEL_PREFIXES of run_robotiq_client.py): always on, whatever the
# site file lists besides.
RESTRICTED_PREFIXES = ("pi05_fr3_sp",)
# The only task instructions such a model was trained on (kept with the site
# file's constants, which the public view of the site shares).
RESTRICTED_PROMPTS = rig_site.RESTRICTED_PROMPTS
TTL_S = 30.0
UNVERIFIED = "unverified"
_WARNING = re.compile(r"[A-Za-z0-9_:.,=-]+")
# The longest rollout group the client command form accepts
# (``levi/rig/command.py`` builds its pattern from this number).
GROUP_MAX_CHARS = 80


@dataclass(frozen=True)
class Model:
    id: str  # the checkpoint folder's name
    config: str  # the serving config (--policy.config)
    cfg_capable: bool  # takes --cfg-guidance-scale
    restricted: bool  # takes only the client's known task instructions
    verified: bool  # a real-GPU serving check is recorded in the site file
    step: int | None
    version: str | None
    variant: str | None
    size_bytes: int
    warnings: tuple  # policies.PolicyEntry.warnings codes, without paths

    @property
    def labels(self) -> tuple:
        return () if self.verified else (UNVERIFIED,)

    def view(self) -> dict:
        return {
            "id": self.id,
            "config": self.config,
            "cfg_capable": self.cfg_capable,
            "restricted_prompts": self.restricted,
            "verified": self.verified,
            "labels": list(self.labels),
            "step": self.step,
            "version": self.version,
            "variant": self.variant,
            "size_bytes": self.size_bytes,
            "warnings": list(self.warnings),
            "rollout_group_default": {
                "rtc": default_group(self.id, True),
                "plain": default_group(self.id, False),
            },
        }


@dataclass(frozen=True)
class Catalog:
    models: tuple  # sorted by id
    skipped: tuple  # ({"id", "reason"}, ...) sorted by id
    problems: tuple  # root-level codes, e.g. "root_missing"


def default_group(model_id: str, rtc: bool) -> str:
    """The rollout group a run of this model is filed under by default."""
    return f"{model_id}_rtc_demo" if rtc else f"{model_id}_demo"


def default_group_fits(model_id: str) -> bool:
    """Whether the default rollout group of this model, in either mode, is one
    the client command form accepts (``<id>_rtc_demo`` is the longer one)."""
    return all(
        len(default_group(model_id, rtc)) <= GROUP_MAX_CHARS for rtc in (True, False)
    )


def _top_level_norm_stats(entry) -> bool:
    try:
        return stat.S_ISREG(os.lstat(Path(entry.path) / "norm_stats.json").st_mode)
    except OSError:
        return False


def _printable(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "?", name)[:80]


def _reason(entry, site):
    """Why the checkpoint is not offered, or ``None``."""
    if not rig_site.NAME.fullmatch(entry.name) or ".." in entry.name:
        return "unsafe_name"
    if not default_group_fits(entry.name):
        # Offered, its default form would be refused: the group is too long.
        return "name_too_long"
    if entry.name in site.catalog.excluded:
        return "excluded_by_site"
    if not entry.deployable:
        return "not_deployable"
    if entry.kind != "jax":
        return "not_jax"
    if entry.role != "forward":
        return "role_not_forward"
    if entry.config not in site.catalog.known_configs:
        return "config_unknown"
    if not _top_level_norm_stats(entry):
        return "no_toplevel_norm_stats"
    return None


def _model(entry, site) -> Model:
    restricted = RESTRICTED_PREFIXES + site.catalog.restricted_prefixes
    return Model(
        id=entry.name,
        config=entry.config,
        cfg_capable=entry.config in site.catalog.cfg_configs,
        restricted=entry.name.startswith(restricted)
        or entry.config.startswith(restricted),
        verified=entry.name in site.catalog.verified,
        step=entry.step,
        version=entry.version,
        variant=entry.variant,
        size_bytes=entry.size_bytes,
        warnings=tuple(w for w in entry.warnings if _WARNING.fullmatch(w)),
    )


def build(site, discover=None) -> Catalog:
    """Read the checkpoints folder and sort what is in it (no cache)."""
    discover = discover or policies.discover_report
    # The site names the configs of the forward family; the rest of the
    # recipe (which folder serves under which config) stays the default.
    report = discover(
        site.checkpoints_root,
        recipe={"forward_configs": list(site.catalog.known_configs)},
    )
    models, skipped = [], []
    for entry in report.entries:
        reason = _reason(entry, site)
        if reason is None:
            models.append(_model(entry, site))
        else:
            skipped.append({"id": _printable(entry.name), "reason": reason})
    problems = list(report.problems) + (
        ["entries_truncated"] if report.truncated else []
    )
    return Catalog(
        models=tuple(sorted(models, key=lambda m: m.id)),
        skipped=tuple(sorted(skipped, key=lambda s: s["id"])),
        problems=tuple(problems),
    )


_LOCK = threading.Lock()
_CACHED: dict = {}  # "key", "at", "catalog"


def cache_clear() -> None:
    with _LOCK:
        _CACHED.clear()


def current(site, now=None, discover=None) -> Catalog:
    """The catalog, read again when the last read is 30 seconds old or the
    site's catalog settings or folder differ. ``now`` is a monotonic time."""
    now = time.monotonic() if now is None else now
    key = (site.checkpoints_root, site.catalog)
    with _LOCK:
        if _CACHED.get("key") == key and now - _CACHED["at"] < TTL_S:
            return _CACHED["catalog"]
    catalog = build(site, discover)
    with _LOCK:
        _CACHED.update(key=key, at=now, catalog=catalog)
    return catalog


def get(site, model_id, now=None, discover=None):
    """The offered model with this id, or ``None``."""
    return next(
        (m for m in current(site, now, discover).models if m.id == model_id), None
    )


def view(catalog: Catalog, site) -> dict:
    """``GET /api/levi/rig/models``."""
    return {
        "schema": SCHEMA,
        "models": [m.view() for m in catalog.models],
        "skipped": [dict(s) for s in catalog.skipped],
        "problems": list(catalog.problems),
        "defaults": {
            "rtc": True,
            "fraction": site.gpu.fraction_presets[0],
            "cfg_guidance_scale": site.caps.cfg_scale_default,
        },
    }
