"""A03-aware parser for ENTSO-E publication and load XML documents.

Structure adapted from the author's `entsoe-transparency` project (`xml_parser.py`):
namespace-agnostic element access and the set of kept attributes. New here: expansion of
compressed curves (A03) using the Period end, parsing of the document request window, and
columnar output.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa

from .a03 import expand_positions

PRICES = "day_ahead_prices"
LOAD = "actual_load"
DATASETS = (PRICES, LOAD)

PARSED_SCHEMA = pa.schema(
    [
        ("dataset", pa.string()),
        ("zone_key", pa.string()),
        ("timestamp_utc", pa.timestamp("us", tz="UTC")),
        ("resolution", pa.string()),
        ("sequence", pa.int32()),
        ("value", pa.float64()),
        ("currency", pa.string()),
        ("unit", pa.string()),
        ("curve_type", pa.string()),
        ("is_a03_filled", pa.bool_()),
        ("business_type", pa.string()),
        ("contract_type", pa.string()),
        ("auction_type", pa.string()),
        ("document_id", pa.string()),
        ("revision_number", pa.int32()),
        ("document_created_utc", pa.timestamp("us", tz="UTC")),
        ("request_start_utc", pa.timestamp("us", tz="UTC")),
        ("request_end_utc", pa.timestamp("us", tz="UTC")),
        ("period_start_utc", pa.timestamp("us", tz="UTC")),
        ("period_end_utc", pa.timestamp("us", tz="UTC")),
        ("timeseries_id", pa.string()),
        ("source_file", pa.string()),
    ]
)
COLUMNS = PARSED_SCHEMA.names


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def direct(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if local_name(child.tag) == name), None)


def direct_text(element: ET.Element, name: str) -> str | None:
    child = direct(element, name)
    return child.text.strip() if child is not None and child.text else None


def nested_text(element: ET.Element, *names: str) -> str | None:
    current: ET.Element | None = element
    for name in names:
        current = direct(current, name) if current is not None else None
    return current.text.strip() if current is not None and current.text else None


def parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        raise ValueError(f"Unsupported ENTSO-E resolution: {value}")
    hours, minutes, seconds = (int(item or 0) for item in match.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


@dataclass
class ParseStats:
    rows: int = 0
    filled: int = 0


def parse_document(
    path: Path, dataset: str, zone_key: str, source_name: str | None = None
) -> tuple[pa.Table, ParseStats]:
    """Parse one XML document into a table with expanded A03 curves."""
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset: {dataset}")
    cols: dict[str, list[object]] = {name: [] for name in COLUMNS}
    stats = ParseStats()
    root = ET.parse(path).getroot()
    if local_name(root.tag) == "Acknowledgement_MarketDocument":
        return pa.Table.from_pylist([], schema=PARSED_SCHEMA), stats

    document_id = direct_text(root, "mRID")
    revision_text = direct_text(root, "revisionNumber")
    revision = int(revision_text) if revision_text and revision_text.isdigit() else None
    created_text = direct_text(root, "createdDateTime")
    created = parse_timestamp(created_text) if created_text else None
    req_start_text = nested_text(root, "period.timeInterval", "start")
    req_end_text = nested_text(root, "period.timeInterval", "end")
    req_start = parse_timestamp(req_start_text) if req_start_text else None
    req_end = parse_timestamp(req_end_text) if req_end_text else None
    source = source_name or path.name
    value_tag = "price.amount" if dataset == PRICES else "quantity"

    for series in (e for e in root if local_name(e.tag) == "TimeSeries"):
        contract_type = direct_text(series, "contract_MarketAgreement.type")
        if dataset == PRICES and contract_type not in {None, "A01"}:
            continue
        business_type = direct_text(series, "businessType")
        auction_type = direct_text(series, "auction.type")
        sequence_text = direct_text(
            series, "classificationSequence_AttributeInstanceComponent.position"
        )
        sequence = int(sequence_text) if sequence_text else None
        unit = direct_text(series, "quantity_Measure_Unit.name")
        if dataset == PRICES:
            unit = direct_text(series, "price_Measure_Unit.name") or unit
        currency = direct_text(series, "currency_Unit.name")
        curve_type = direct_text(series, "curveType")
        series_id = direct_text(series, "mRID") or ""

        for period in (e for e in series if local_name(e.tag) == "Period"):
            start_text = nested_text(period, "timeInterval", "start")
            end_text = nested_text(period, "timeInterval", "end")
            resolution = direct_text(period, "resolution")
            if not start_text or not end_text or not resolution:
                continue
            start, end = parse_timestamp(start_text), parse_timestamp(end_text)
            step = parse_duration(resolution)
            slot_count = int((end - start) / step)
            points: dict[int, float] = {}
            for point in (e for e in period if local_name(e.tag) == "Point"):
                position_text = direct_text(point, "position")
                value_text = direct_text(point, value_tag)
                if position_text and value_text is not None:
                    points[int(position_text)] = float(value_text)
            for position, value, filled in expand_positions(
                points, slot_count, compressed=curve_type == "A03"
            ):
                cols["dataset"].append(dataset)
                cols["zone_key"].append(zone_key)
                cols["timestamp_utc"].append(start + step * (position - 1))
                cols["resolution"].append(resolution)
                cols["sequence"].append(sequence)
                cols["value"].append(value)
                cols["currency"].append(currency)
                cols["unit"].append(unit)
                cols["curve_type"].append(curve_type)
                cols["is_a03_filled"].append(filled)
                cols["business_type"].append(business_type)
                cols["contract_type"].append(contract_type)
                cols["auction_type"].append(auction_type)
                cols["document_id"].append(document_id)
                cols["revision_number"].append(revision)
                cols["document_created_utc"].append(created)
                cols["request_start_utc"].append(req_start)
                cols["request_end_utc"].append(req_end)
                cols["period_start_utc"].append(start)
                cols["period_end_utc"].append(end)
                cols["timeseries_id"].append(series_id)
                cols["source_file"].append(source)
                stats.rows += 1
                stats.filled += filled
    return pa.table(cols, schema=PARSED_SCHEMA), stats
