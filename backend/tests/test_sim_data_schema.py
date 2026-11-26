"""V2 dataset schema — migration 062 structural tests (no network).

Run from backend/: pytest tests/test_sim_data_schema.py -v
"""
from __future__ import annotations

from pathlib import Path


def load_migration(name: str) -> str:
    path = Path(__file__).resolve().parents[2] / "database" / name
    return path.read_text(encoding="utf-8").lower()


def test_v2_tables_have_versioned_primary_key():
    schema = load_migration("062_sim_price_data_v2.sql")
    assert "primary key (dataset_version, asset, bar_date)" in schema
    assert "source in ('biquote', 'twelvedata')" in schema


def test_v2_tables_are_service_role_only():
    schema = load_migration("062_sim_price_data_v2.sql")
    assert "enable row level security" in schema
    assert "revoke all on sim_price_bars_v2 from anon, authenticated" in schema
