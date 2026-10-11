"""Expansion of ENTSO-E compressed curves (curveType A03)."""

from __future__ import annotations

from collections.abc import Iterator


def expand_positions(
    points: dict[int, float], slot_count: int, *, compressed: bool
) -> Iterator[tuple[int, float, bool]]:
    """Yield (position, value, is_filled) for every slot of a Period.

    With curveType A03 an omitted position repeats the previous value until the Period end.
    Other curve types keep their gaps. A missing first position has nothing to repeat and
    stays missing.
    """
    last: float | None = None
    for position in range(1, slot_count + 1):
        if position in points:
            last = points[position]
            yield position, last, False
        elif compressed and last is not None:
            yield position, last, True
    # Points beyond the Period end are kept as published; silver drops them if out of window.
    for position in sorted(p for p in points if p > slot_count):
        yield position, points[position], False
