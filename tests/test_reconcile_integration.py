from __future__ import annotations

import pytest

from entsoe_observatory.reconcile import reconcile
from entsoe_observatory.settings import Settings

pytestmark = pytest.mark.integration


def test_silver_has_more_rows_and_no_legacy_key_is_lost() -> None:
    """Run with `pytest -m integration` after a full backfill (needs LEGACY_PROCESSED_DIR)."""
    settings = Settings()
    if settings.legacy_processed_dir is None or not settings.legacy_processed_dir.exists():
        pytest.skip("legacy snapshot not available")
    for row in reconcile(settings):
        assert row["added_rows"] > 0, row
        assert row["legacy_keys_missing_in_silver"] == 0, row
