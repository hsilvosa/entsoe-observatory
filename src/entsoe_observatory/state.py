"""Pipeline state in object storage, so stateless CI runners can run incremental updates.

`push_state` uploads bronze, silver, landing and dq files that changed, keyed by sha256 in
`_state/manifest.json`. `pull_state` restores them on an empty runner. Gold is not part of the
state: it is rebuilt from silver and published separately.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from .settings import Settings
from .storage import sha256_file

STATE_MANIFEST = "_state/manifest.json"
STATE_LAYERS = ("landing", "bronze", "silver", "dq")


class StateStore(Protocol):
    def get_text(self, key: str) -> str | None: ...
    def put_file(self, key: str, path: Path, content_type: str) -> None: ...
    def put_text(self, key: str, text: str, content_type: str) -> None: ...
    def get_file(self, key: str, path: Path) -> None: ...
    def list_sizes(self, prefix: str) -> dict[str, int]: ...


def _local_files(settings: Settings) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for layer in STATE_LAYERS:
        base = settings.layer_dir(layer)
        if base.exists():
            for path in sorted(base.rglob("*")):
                if path.is_file() and not any(p.endswith(".tmp") for p in path.parts):
                    files[f"_state/{path.relative_to(settings.storage_root).as_posix()}"] = path
    return files


def _remote_manifest(store: StateStore) -> dict[str, Any]:
    raw = store.get_text(STATE_MANIFEST)
    return json.loads(raw)["files"] if raw else {}


def push_state(settings: Settings, store: StateStore, workers: int = 16) -> dict[str, int]:
    remote = _remote_manifest(store)
    local = _local_files(settings)
    current: dict[str, Any] = {}
    todo: list[str] = []
    for key, path in local.items():
        size = path.stat().st_size
        known = remote.get(key)
        if known and known["bytes"] == size and known.get("mtime") == path.stat().st_mtime_ns:
            current[key] = known
            continue
        digest = sha256_file(path)
        current[key] = {"sha256": digest, "bytes": size, "mtime": path.stat().st_mtime_ns}
        if not known or known["sha256"] != digest:
            todo.append(key)
    if todo and not remote:
        # An interrupted first upload leaves objects without a manifest: skip equal sizes.
        present = store.list_sizes("_state/")
        todo = [k for k in todo if present.get(k) != current[k]["bytes"]]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(
            lambda k: store.put_file(k, local[k], "application/octet-stream"), todo
        ))
    uploaded = len(todo)
    # Files that no longer exist locally (replaced partitions) are dropped from the manifest.
    store.put_text(STATE_MANIFEST, json.dumps({"files": current}), "application/json")
    return {"files": len(current), "uploaded": uploaded, "dropped": len(set(remote) - set(current))}


def pull_state(settings: Settings, store: StateStore) -> dict[str, int]:
    remote = _remote_manifest(store)
    downloaded = 0
    for key, info in remote.items():
        target = settings.storage_root / key.removeprefix("_state/")
        if target.exists() and target.stat().st_size == info["bytes"]:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        store.get_file(key, target)
        downloaded += 1
    return {"files": len(remote), "downloaded": downloaded}
