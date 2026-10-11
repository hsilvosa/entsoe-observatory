"""Command line interface."""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from .bronze import build_bronze, build_landing_manifest, new_run_id
from .client import EntsoeClient
from .dashboard import build_dashboard
from .dq import DataQualityError, run_checks
from .events import GridEventV2
from .fx import pull_fx
from .gold import ContractViolation, build_gold
from .live import pull_live
from .publish import S3Store, publish_huggingface, publish_r2
from .reconcile import reconcile as run_reconcile
from .settings import Settings
from .silver import build_silver
from .weather import WeatherClient, pull_weather

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


@app.command()
def pull(
    zone: ZoneOption = None,
    dataset: DatasetOption = None,
    lookback_days: Annotated[int, typer.Option(help="Days re-read before the newest data.")] = 3,
    initial_days: Annotated[int, typer.Option(help="Window for zones without data.")] = 7,
) -> None:
    """Incremental live pull from the ENTSO-E API (needs ENTSOE_API_TOKEN or ENTSOE_ENV_FILE)."""
    settings = Settings()
    client = EntsoeClient(settings.resolve_token())
    started = time.perf_counter()
    summary = pull_live(
        settings, client, zones=zone, datasets=dataset, lookback_days=lookback_days,
        initial_days=initial_days, on_progress=typer.echo,
    )
    client.close()
    _echo("pull", {k: v for k, v in summary.items() if k != "pulls"}, started)
    if summary["errors"]:
        raise typer.Exit(code=1)


@app.command()
def weather(
    zone: ZoneOption = None,
    start: Annotated[str | None, typer.Option(help="YYYY-MM-DD, default 7 days ago.")] = None,
    end: Annotated[str | None, typer.Option(help="YYYY-MM-DD, default today.")] = None,
    resume: Annotated[bool, typer.Option(help="Skip zones that already span the range.")] = False,
) -> None:
    """Pull Open-Meteo weather for the zone points."""
    today = datetime.now(UTC).date()
    begin = date.fromisoformat(start) if start else today - timedelta(days=7)
    last = date.fromisoformat(end) if end else today
    client = WeatherClient()
    started = time.perf_counter()
    summary = pull_weather(
        Settings(), client, start=begin, end=last, zones=zone, skip_covered=resume,
        on_progress=typer.echo,
    )
    client.close()
    _echo("weather", {"rows": summary["rows"], "pending": summary["pending_zones"]}, started)
    if summary["pending_zones"]:
        raise typer.Exit(code=2)


@app.command()
def fx() -> None:
    """Pull ECB reference rates for the non-EUR currencies of the registry."""
    started = time.perf_counter()
    _echo("fx", pull_fx(Settings()), started)


@app.command()
def dq(strict: Annotated[bool, typer.Option(help="Exit 1 on blocking failures.")] = True) -> None:
    """Run the data quality checks and store the results under dq/."""
    started = time.perf_counter()
    try:
        report = run_checks(Settings(), fail_on_blocking=strict)
    except DataQualityError as error:
        typer.echo(f"dq FAILED: {error}")
        raise typer.Exit(code=1) from None
    _echo(
        "dq",
        {
            "run_id": report.run_id,
            "checks": report.checks_run,
            "blocking_failures": len(report.blocking_failures),
            "warning_failures": report.warning_failures,
        },
        started,
    )


@app.command()
def contracts() -> None:
    """Print the JSON schema of the event contract v2."""
    typer.echo(json.dumps(GridEventV2.model_json_schema(), indent=2, sort_keys=True))


@app.command()
def gold(
    table: Annotated[list[str] | None, typer.Option("--table", help="Only this table.")] = None,
) -> None:
    """Build the gold tables from silver and enforce their contracts."""
    started = time.perf_counter()
    try:
        summary = build_gold(Settings(), tables=table, on_progress=typer.echo)
    except ContractViolation as error:
        typer.echo(f"gold FAILED: {error}")
        raise typer.Exit(code=1) from None
    _echo("gold", {"rows": summary["rows"], "mb": round(summary["bytes"] / 2**20, 1),
                   "skipped": summary["skipped"]}, started)


@app.command()
def dashboard(
    out: Annotated[Path | None, typer.Option(help="Default: STORAGE_ROOT/site.")] = None,
) -> None:
    """Build the static dashboard from the gold tables."""
    settings = Settings()
    started = time.perf_counter()
    _echo("dashboard", build_dashboard(settings, out or settings.storage_root / "site"), started)


@app.command("publish-r2")
def publish_r2_command(
    dry_run: Annotated[bool, typer.Option(help="Only show what would change.")] = False,
    prune: Annotated[bool, typer.Option(help="Delete remote files that are gone locally.")] = False,
) -> None:
    """Sync gold, contracts and the dataset card to Cloudflare R2."""
    settings = Settings()
    started = time.perf_counter()
    store = None if dry_run else S3Store(settings)
    summary = publish_r2(settings, store or _DryStore(), dry_run=dry_run, prune=prune)  # type: ignore[arg-type]
    _echo("publish-r2", summary, started)


@app.command("publish-hf")
def publish_hf_command(
    dry_run: Annotated[bool, typer.Option(help="Only stage and count files.")] = False,
) -> None:
    """Upload gold, contracts and the dataset card to a Hugging Face dataset repository."""
    started = time.perf_counter()
    _echo("publish-hf", publish_huggingface(Settings(), dry_run=dry_run), started)


@app.command()
def card() -> None:
    """Write the dataset card to STORAGE_ROOT/publish/README.md."""
    from .dataset_card import build_card

    settings = Settings()
    target = settings.layer_dir("publish") / "README.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_card(settings), encoding="utf-8", newline="\n")
    typer.echo(str(target))


class _DryStore:
    """Store used for dry runs: nothing is remote, so every file counts as new."""

    def get_text(self, key: str) -> str | None:
        return None
