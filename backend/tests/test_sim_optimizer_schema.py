"""Optimizer schema — migration 063 structural tests (no live DB).

Run from backend/: pytest tests/test_sim_optimizer_schema.py -v
"""
from __future__ import annotations

from pathlib import Path


def load_migration(name: str) -> str:
    path = Path(__file__).resolve().parents[2] / "database" / name
    return path.read_text(encoding="utf-8").lower()


def test_optimizer_schema_is_service_role_only():
    schema = load_migration("063_sim_optimizer.sql")
    assert "create table if not exists sim_opt_runs" in schema
    assert "revoke all on sim_opt_runs from anon, authenticated" in schema


def test_optimizer_schema_has_core_tables():
    schema = load_migration("063_sim_optimizer.sql")
    assert "create table if not exists sim_opt_iterations" in schema
    assert "create table if not exists sim_opt_trades" in schema
    assert "create table if not exists sim_opt_equity" in schema
    assert "enable row level security" in schema
