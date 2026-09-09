"""rr_target setting — Reward:Risk target flows from Settings into signals."""
import pytest

from app.models.schemas import AppSettings
from tests.test_settings import SettingsDatabase, call, set_state


def test_rr_target_default_is_two():
    s = AppSettings()
    assert s.rr_target == 2.0


@pytest.mark.asyncio
async def test_put_settings_persists_rr_target():
    """rr_target round-trips through the settings API."""
    db = SettingsDatabase(None)
    set_state(db)
    res = await call("PUT", "/api/settings", {"rr_target": 3.0})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["settings"]["rr_target"] == 3.0
    assert db._client.row["rr_target"] == 3.0


@pytest.mark.asyncio
async def test_get_settings_returns_rr_target_from_row():
    row = AppSettings(rr_target=1.5)
    set_state(SettingsDatabase(row.model_dump(mode="json")))
    res = await call("GET", "/api/settings")
    assert res.status_code == 200
    assert res.json()["rr_target"] == 1.5
