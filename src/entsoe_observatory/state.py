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


ALWAYS = ("live_manifest.jsonl",)


def _in_scope(key: str, only: list[str] | None) -> bool:
    """Scoped runs (for example one dataset) only touch keys that contain a given marker."""
    return only is None or any(f"/{name}/" in key for name in only) or key.endswith(ALWAYS)


def push_state(
    settings: Settings, store: StateStore, workers: int = 16, only: list[str] | None = None
) -> dict[str, int]:
    full_remote = _remote_manifest(store)
    remote = {k: v for k, v in full_remote.items() if _in_scope(k, only)}
    outside = {k: v for k, v in full_remote.items() if k not in remote}
    local = {k: p for k, p in _local_files(settings).items() if _in_scope(k, only)}
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
    store.put_text(
        STATE_MANIFEST, json.dumps({"files": {**outside, **current}}), "application/json"
    )
    return {"files": len(current), "uploaded": uploaded, "dropped": len(set(remote) - set(current))}


def pull_state(
    settings: Settings, store: StateStore, workers: int = 32, only: list[str] | None = None
) -> dict[str, int]:
    remote = {k: v for k, v in _remote_manifest(store).items() if _in_scope(k, only)}
    todo: list[tuple[str, Path]] = []
    for key, info in remote.items():
        target = settings.storage_root / key.removeprefix("_state/")
        if not (target.exists() and target.stat().st_size == info["bytes"]):
            target.parent.mkdir(parents=True, exist_ok=True)
            todo.append((key, target))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda item: store.get_file(*item), todo))
    return {"files": len(remote), "downloaded": len(todo)}
