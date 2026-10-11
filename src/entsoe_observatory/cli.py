"""Command line interface."""

from __future__ import annotations

import json
import time
from typing import Annotated

import typer

from .bronze import build_bronze, build_landing_manifest, new_run_id
from .reconcile import reconcile as run_reconcile
from .settings import Settings
from .silver import build_silver

app = typer.Typer(no_args_is_help=True, add_completion=False)

ZoneOption = Annotated[list[str] | None, typer.Option("--zone", help="Limit to a zone key.")]
DatasetOption = Annotated[
    list[str] | None, typer.Option("--dataset", help="day_ahead_prices or actual_load.")
]


def _echo(label: str, body: object, started: float) -> None:
    typer.echo(f"{label} ({time.perf_counter() - started:.1f}s): {json.dumps(body, default=str)}")


@app.command()
def landing() -> None:
    """Checksum the raw XML files into the landing manifest."""
    started = time.perf_counter()
    entries = build_landing_manifest(Settings(), new_run_id())
    _echo("landing", {"files": len(entries)}, started)


@app.command()
def bronze(zone: ZoneOption = None, dataset: DatasetOption = None) -> None:
    """Parse raw XML (A03 expanded) into the bronze layer."""
    started = time.perf_counter()
    _echo("bronze", build_bronze(Settings(), new_run_id(), zones=zone, datasets=dataset), started)


@app.command()
def silver(zone: ZoneOption = None, dataset: DatasetOption = None) -> None:
    """Build the silver layer from bronze."""
    started = time.perf_counter()
    _echo("silver", build_silver(Settings(), new_run_id(), zones=zone, datasets=dataset), started)


@app.command()
def backfill(zone: ZoneOption = None, dataset: DatasetOption = None) -> None:
    """Landing manifest, bronze and silver in one run."""
    settings = Settings()
    run_id = new_run_id()
    started = time.perf_counter()
    entries = build_landing_manifest(settings, run_id)
    _echo("landing", {"files": len(entries)}, started)
    started = time.perf_counter()
    _echo("bronze", build_bronze(settings, run_id, zones=zone, datasets=dataset), started)
    started = time.perf_counter()
    _echo("silver", build_silver(settings, run_id, zones=zone, datasets=dataset), started)


@app.command()
def reconcile() -> None:
    """Compare silver with the legacy parquet snapshot (LEGACY_PROCESSED_DIR)."""
    typer.echo(json.dumps(run_reconcile(Settings()), indent=1))
