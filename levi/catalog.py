"""Persistent dataset registration and review manifests, with atomic writes."""

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from .paths import STATE, inside

LOCK = threading.RLock()
DEMOS = ["samanthalhy/so100_strawberry_2", "samanthalhy/eval_so100_smol_strawberry_2"]


def read(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    os.replace(temp, path)


def datasets():
    return read(STATE / "datasets.json", {})


def register(path: str):
    root = inside(path)
    info = read(inside("meta/info.json", root), None)
    if not info or "features" not in info or "fps" not in info:
        raise ValueError("A LeRobot dataset needs meta/info.json with features and fps")
    slug = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    item = {"id": "local/" + slug, "path": str(root), "name": root.name, "info": info}
    with LOCK:
        all_items = datasets()
        all_items[slug] = item
        atomic(STATE / "datasets.json", all_items)
    return item


def local_root(repo: str):
    if not repo.startswith("local/"):
        return None
    item = datasets().get(repo.split("/", 1)[1])
    if not item:
        raise ValueError("Local dataset is not registered")
    return inside(item["path"])


def review_path(repo: str):
    return STATE / "reviews" / (hashlib.sha256(repo.encode()).hexdigest() + ".json")
