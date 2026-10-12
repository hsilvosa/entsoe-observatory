from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import Pipeline
from test_gold import scenario  # noqa: F401  (shared fixture)

from entsoe_observatory.dataset_card import build_card
from entsoe_observatory.gold import TABLE_ORDER
from entsoe_observatory.publish import (
    MANIFEST_KEY,
    S3Store,
    publish_huggingface,
    publish_r2,
)
from entsoe_observatory.settings import Settings


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    def get_text(self, key: str) -> str | None:
        data = self.objects.get(key)
        return None if data is None else data.decode("utf-8")

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        self.objects[key] = path.read_bytes()
        self.puts.append(key)

    def put_text(self, key: str, text: str, content_type: str) -> None:
        self.objects[key] = text.encode("utf-8")

    def delete(self, keys: list[str]) -> None:
        for key in keys:
            self.objects.pop(key, None)


def test_card_has_front_matter_tables_and_limitations(scenario: Pipeline) -> None:  # noqa: F811
    card = build_card(scenario.settings)
    assert card.startswith("---\n")
    assert "license: other" in card
    for table in TABLE_ORDER:
        if table == "weather_hourly":
            continue
        assert f"config_name: {table}" in card
        assert f"### `{table}`" in card
    assert "## Known limitations" in card and "ENTSO-E Transparency Platform" in card
    assert "A03" in card
    assert all(ord(ch) < 0x2190 for ch in card)  # no emoji or pictographs


def test_r2_sync_is_incremental_and_ordered(scenario: Pipeline) -> None:  # noqa: F811
    store = MemoryStore()
    first = publish_r2(scenario.settings, store)
    assert first["upload"] > 10 and first["unchanged"] == 0
    assert MANIFEST_KEY in store.objects
    assert {"README.md", "contracts/price_native.json"} <= set(store.objects)
    assert any(k.startswith("data/price_native/") for k in store.objects)

    store.puts.clear()
    second = publish_r2(scenario.settings, store)
    assert second["upload"] == 0 and store.puts == []

    victim = next(k for k in store.objects if k.startswith("data/zone_daily/"))
    manifest = json.loads(store.objects[MANIFEST_KEY].decode())
    manifest["files"][victim]["sha256"] = "stale"
    store.objects[MANIFEST_KEY] = json.dumps(manifest).encode()
    third = publish_r2(scenario.settings, store)
    assert third["upload"] == 1 and store.puts == [victim]


def test_r2_prune_removes_files_that_are_gone(scenario: Pipeline) -> None:  # noqa: F811
    store = MemoryStore()
    publish_r2(scenario.settings, store)
    manifest = json.loads(store.objects[MANIFEST_KEY].decode())
    manifest["files"]["data/old_table/part.parquet"] = {"sha256": "x", "bytes": 1}
    store.objects[MANIFEST_KEY] = json.dumps(manifest).encode()
    store.objects["data/old_table/part.parquet"] = b"x"
    kept = publish_r2(scenario.settings, store, prune=False)
    assert kept["delete"] == 0 and "data/old_table/part.parquet" in store.objects
    pruned = publish_r2(scenario.settings, store, prune=True)
    assert pruned["delete"] == 1 and "data/old_table/part.parquet" not in store.objects
    assert "data/old_table/part.parquet" not in json.loads(store.objects[MANIFEST_KEY])["files"]


def test_dry_run_changes_nothing(scenario: Pipeline) -> None:  # noqa: F811
    store = MemoryStore()
    summary = publish_r2(scenario.settings, store, dry_run=True)
    assert summary["upload"] > 10 and store.objects == {}


def test_secrets_never_reach_manifests(scenario: Pipeline) -> None:  # noqa: F811
    secret = "very-secret-value-123"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, STORAGE_ROOT=scenario.settings.storage_root, R2_SECRET_ACCESS_KEY=secret,
        R2_ACCESS_KEY_ID="key-id-456", HF_TOKEN="hf-token-789",
    )
    store = MemoryStore()
    publish_r2(settings, store)
    for path in settings.layer_dir("manifests").glob("*"):
        text = path.read_text(encoding="utf-8")
        assert secret not in text and "key-id-456" not in text and "hf-token-789" not in text
    assert secret not in repr(settings) and "hf-token-789" not in repr(settings)
    assert secret not in json.dumps(json.loads(store.objects[MANIFEST_KEY].decode()))


def test_s3_store_needs_complete_settings(tmp_path: Path) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, STORAGE_ROOT=tmp_path, R2_BUCKET="b"
    )
    with pytest.raises(ValueError, match="R2_ACCOUNT_ID"):
        S3Store(settings)


class FakeHfApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.staged: list[str] = []

    def create_repo(self, repo_id: str, **kwargs: Any) -> None:
        self.calls.append(("create_repo", {"repo_id": repo_id, **kwargs}))

    def upload_folder(self, **kwargs: Any) -> str:
        folder = Path(kwargs["folder_path"])
        self.staged = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")
                             if p.is_file())
        self.calls.append(("upload_folder", kwargs))
        return "commit-abc"


def test_huggingface_upload_stages_data_contracts_and_card(scenario: Pipeline) -> None:  # noqa: F811
    api = FakeHfApi()
    summary = publish_huggingface(scenario.settings, api)
    assert summary["commit"] == "commit-abc"
    assert api.calls[0][0] == "create_repo"
    assert api.calls[0][1]["repo_type"] == "dataset" and api.calls[0][1]["exist_ok"] is True
    assert "README.md" in api.staged and "contracts/zone_daily.json" in api.staged
    assert any(p.startswith("data/price_hourly/") for p in api.staged)


def test_huggingface_dry_run_makes_no_api_call(scenario: Pipeline) -> None:  # noqa: F811
    api = FakeHfApi()
    summary = publish_huggingface(scenario.settings, api, dry_run=True)
    assert api.calls == [] and summary["files"] > 10


def test_card_counts_rows_from_files_after_a_partial_rebuild(scenario: Pipeline) -> None:  # noqa: F811
    from entsoe_observatory.gold import build_gold

    build_gold(scenario.settings, tables=["zone_daily"])  # newest manifest lists one table only
    card = build_card(scenario.settings)
    assert "### `price_native`" in card and "### `zone_daily`" in card
