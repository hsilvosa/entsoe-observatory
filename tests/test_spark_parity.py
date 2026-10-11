from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pandas
import pytest
from conftest import Pipeline
from xmlgen import DocSpec, PeriodSpec, SeriesSpec

from entsoe_observatory.silver_spark import silver_zone_spark

pytestmark = pytest.mark.spark
LOCAL_HADOOP = Path(os.environ.get("LOCALAPPDATA", "")) / "hadoop-3.3.6"


@pytest.fixture(scope="module")
def spark() -> Iterator[Any]:
    pyspark_sql = pytest.importorskip("pyspark.sql")
    os.environ.pop("SPARK_HOME", None)
    os.environ["PYSPARK_PYTHON"] = sys.executable
    if os.name == "nt":
        candidates = [LOCAL_HADOOP, Path(os.environ.get("HADOOP_HOME", "."))]
        hadoop = next((c for c in candidates if (c / "bin" / "hadoop.dll").exists()), candidates[0])
        if not (hadoop / "bin" / "hadoop.dll").exists():
            pytest.skip("Hadoop Windows binaries not available")
        os.environ["HADOOP_HOME"] = str(hadoop)
        os.environ["PATH"] = str(hadoop / "bin") + os.pathsep + os.environ["PATH"]
    session = (
        pyspark_sql.SparkSession.builder.master("local[2]")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    yield session
    session.stop()


def sample_docs() -> list[tuple[str, str, str, DocSpec]]:
    """Revisions, overlapping files, A03 gaps, DST day, two sequences, negative load."""
    s, e = "2025-10-25T22:00Z", "2025-10-26T23:00Z"
    quarter = PeriodSpec(s, e, "PT15M", {1: 10.0, 30: 12.0, 70: 9.0})
    old = DocSpec([SeriesSpec([quarter], sequence=1)], s, e, revision=1, mrid="old")
    new = DocSpec(
        [SeriesSpec([quarter], sequence=1), SeriesSpec([quarter], sequence=2)],
        s, e, revision=2, mrid="new",
    )
    load_period = PeriodSpec(s, e, "PT60M", {1: -3.0, 2: 5.0, 10: 7.0})
    load = DocSpec([SeriesSpec([load_period])], s, e, dataset="actual_load")
    return [
        ("day_ahead_prices", "AT", "a.xml", old),
        ("day_ahead_prices", "AT", "b.xml", new),
        ("actual_load", "AT", "l.xml", load),
    ]


@pytest.mark.parametrize("dataset", ["day_ahead_prices", "actual_load"])
def test_spark_matches_duckdb(
    spark: Any, run_pipeline: Callable[..., Pipeline], dataset: str
) -> None:
    pipeline = run_pipeline(sample_docs())
    bronze = (pipeline.settings.layer_dir("bronze", dataset) / "zone=AT").as_posix()
    # DataFrame.toPandas needs distutils, which Python 3.12 no longer ships.
    sdf = silver_zone_spark(spark, bronze, "Europe/Vienna").selectExpr(
        "*",
        "date_format(timestamp_local, 'yyyy-MM-dd HH:mm:ss') AS local_str",
        "date_format(timestamp_utc, 'yyyy-MM-dd HH:mm:ss') AS utc_str",
    )
    rows = sdf.collect()
    spark_df = pandas.DataFrame([r.asDict() for r in rows])
    duck_df = (
        pipeline.silver(dataset)
        .execute("select *, strftime(timestamp_local, '%Y-%m-%d %H:%M:%S') AS local_str, "
            "strftime(timestamp_utc, '%Y-%m-%d %H:%M:%S') AS utc_str from s")
        .df()
    )
    cols = [
        "zone_key", "utc_str", "resolution", "sequence", "value", "unit",
        "is_a03_filled", "quality_flag", "is_valid", "revision_number", "local_date",
        "local_str",
    ]
    key = ["utc_str", "resolution", "sequence"]

    def norm(df: Any) -> Any:
        out = df[cols].copy()
        out["local_date"] = out["local_date"].astype(str)
        return out.sort_values(key).reset_index(drop=True)

    left, right = norm(spark_df), norm(duck_df)
    assert len(left) == len(right) > 0
    pandas.testing.assert_frame_equal(left, right, check_dtype=False)
