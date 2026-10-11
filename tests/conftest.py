from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from xmlgen import DocSpec, write_raw_dir

from entsoe_observatory.bronze import build_bronze, build_landing_manifest, new_run_id
from entsoe_observatory.settings import Settings
from entsoe_observatory.silver import build_silver

Files = list[tuple[str, str, str, DocSpec]]


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.summary: dict[str, Any] = {}

    def silver(self, dataset: str) -> duckdb.DuckDBPyConnection:
        glob = (self.settings.layer_dir("silver", dataset) / "**" / "*.parquet").as_posix()
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC'")
        con.execute(
            f"CREATE VIEW s AS SELECT * FROM read_parquet('{glob}', hive_partitioning=false)"
        )
        return con


@pytest.fixture
def run_pipeline(tmp_path: Path) -> Callable[[Files], Pipeline]:
    def run(files: Files) -> Pipeline:
        raw = tmp_path / "raw"
        write_raw_dir(raw, files)
        settings = Settings(STORAGE_ROOT=tmp_path / "storage", ENTSOE_RAW_DIR=raw, THREADS=2)  # type: ignore[call-arg]
        run_id = new_run_id()
        build_landing_manifest(settings, run_id)
        build_bronze(settings, run_id)
        pipeline = Pipeline(settings)
        pipeline.summary = build_silver(settings, run_id)
        return pipeline

    return run
