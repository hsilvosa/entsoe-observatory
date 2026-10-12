from __future__ import annotations

from pathlib import Path

from entsoe_observatory.settings import Settings
from entsoe_observatory.state import STATE_MANIFEST, pull_state, push_state


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

    def list_sizes(self, prefix: str) -> dict[str, int]:
        return {k: len(v) for k, v in self.objects.items() if k.startswith(prefix)}

    def get_file(self, key: str, path: Path) -> None:
        path.write_bytes(self.objects[key])


def settings_for(root: Path) -> Settings:
    return Settings(_env_file=None, STORAGE_ROOT=root)  # type: ignore[call-arg]


def test_state_round_trip_between_runners(tmp_path: Path) -> None:
    runner_a = settings_for(tmp_path / "a")
    for rel in ("silver/day_ahead_prices/zone=ES/year=2026/month=10/p.parquet", "dq/latest.json"):
        path = runner_a.storage_root / rel
        path.parent.mkdir(parents=True)
        path.write_bytes(rel.encode())
    (runner_a.storage_root / "gold").mkdir()
    (runner_a.storage_root / "gold" / "x.parquet").write_bytes(b"gold is not state")
    (runner_a.storage_root / "silver" / "day_ahead_prices" / "zone=ES.tmp").mkdir()
    tmp = runner_a.storage_root / "silver" / "day_ahead_prices" / "zone=ES.tmp"
    (tmp / "t.parquet").write_bytes(b"t")

    store = MemoryStore()
    assert push_state(runner_a, store) == {"files": 2, "uploaded": 2, "dropped": 0}
    assert STATE_MANIFEST in store.objects

    store.puts.clear()
    assert push_state(runner_a, store)["uploaded"] == 0 and store.puts == []

    runner_b = settings_for(tmp_path / "b")
    assert pull_state(runner_b, store) == {"files": 2, "downloaded": 2}
    rel = "silver/day_ahead_prices/zone=ES/year=2026/month=10/p.parquet"
    restored = runner_b.storage_root / rel
    assert restored.read_bytes() == restored.relative_to(runner_b.storage_root).as_posix().encode()
    assert not (runner_b.storage_root / "gold").exists()
    assert pull_state(runner_b, store)["downloaded"] == 0


def test_changed_and_removed_files(tmp_path: Path) -> None:
    runner = settings_for(tmp_path)
    keep = runner.storage_root / "bronze/a.parquet"
    gone = runner.storage_root / "bronze/b.parquet"
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b"one")
    gone.write_bytes(b"two")
    store = MemoryStore()
    push_state(runner, store)
    keep.write_bytes(b"three")
    gone.unlink()
    store.puts.clear()
    result = push_state(runner, store)
    assert store.puts == ["_state/bronze/a.parquet"]
    assert result == {"files": 1, "uploaded": 1, "dropped": 1}


def test_interrupted_first_upload_is_resumed_by_size(tmp_path: Path) -> None:
    runner = settings_for(tmp_path)
    for name in ("a", "b"):
        path = runner.storage_root / f"silver/{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    store = MemoryStore()
    store.objects["_state/silver/a.parquet"] = b"a"  # uploaded before the crash, no manifest
    assert push_state(runner, store)["uploaded"] == 1
    assert store.puts == ["_state/silver/b.parquet"]
