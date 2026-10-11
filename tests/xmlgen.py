"""Builders for synthetic ENTSO-E XML used by the tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"


@dataclass
class PeriodSpec:
    start: str
    end: str
    resolution: str
    points: dict[int, float]


@dataclass
class SeriesSpec:
    periods: list[PeriodSpec]
    curve: str = "A03"
    currency: str = "EUR"
    sequence: int | None = None
    contract_type: str | None = "A01"


@dataclass
class DocSpec:
    series: list[SeriesSpec]
    request_start: str
    request_end: str
    revision: int = 1
    created: str = "2026-01-01T00:00Z"
    mrid: str = "doc1"
    dataset: str = "day_ahead_prices"
    extra: dict[str, str] = field(default_factory=dict)


def render(doc: DocSpec) -> str:
    prices = doc.dataset == "day_ahead_prices"
    out = [
        f'<?xml version="1.0" encoding="utf-8"?><Publication_MarketDocument xmlns="{NS}">',
        f"<mRID>{doc.mrid}</mRID><revisionNumber>{doc.revision}</revisionNumber>",
        f"<createdDateTime>{doc.created}</createdDateTime>",
        f"<period.timeInterval><start>{doc.request_start}</start>"
        f"<end>{doc.request_end}</end></period.timeInterval>",
    ]
    for i, series in enumerate(doc.series, start=1):
        out.append(f"<TimeSeries><mRID>{i}</mRID><businessType>A62</businessType>")
        if series.contract_type:
            out.append(f"<contract_MarketAgreement.type>{series.contract_type}</contract_MarketAgreement.type>")
        if series.sequence is not None:
            out.append(
                "<classificationSequence_AttributeInstanceComponent.position>"
                f"{series.sequence}</classificationSequence_AttributeInstanceComponent.position>"
            )
        if prices:
            out.append(
                f"<currency_Unit.name>{series.currency}</currency_Unit.name>"
                "<price_Measure_Unit.name>MWH</price_Measure_Unit.name>"
            )
        else:
            out.append("<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>")
        out.append(f"<curveType>{series.curve}</curveType>")
        for period in series.periods:
            out.append(
                f"<Period><timeInterval><start>{period.start}</start><end>{period.end}</end>"
                f"</timeInterval><resolution>{period.resolution}</resolution>"
            )
            tag = "price.amount" if prices else "quantity"
            for pos, value in sorted(period.points.items()):
                out.append(f"<Point><position>{pos}</position><{tag}>{value}</{tag}></Point>")
            out.append("</Period>")
        out.append("</TimeSeries>")
    out.append("</Publication_MarketDocument>")
    return "".join(out)


def write_raw_dir(raw_dir: Path, files: list[tuple[str, str, str, DocSpec]]) -> None:
    """files: (dataset, zone_key, filename, doc). Writes XML and a download manifest."""
    tasks = []
    for dataset, zone, name, doc in files:
        rel = f"{dataset}/{name}"
        path = raw_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(doc), encoding="utf-8")
        tasks.append(
            {
                "task_id": f"{dataset}:{zone}:{name}",
                "dataset": dataset,
                "status": "data",
                "file": rel,
                "start": "2025-01-01",
                "end": "2025-02-01",
                "zone": {"key": zone},
            }
        )
    (raw_dir / "download_manifest.json").write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
