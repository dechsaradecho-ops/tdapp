"""Probe: is migration 037 applied, and what is the current gate state?"""
import sys

sys.path.insert(0, r"d:\tdapp\backend")

from app.services import limit_expand  # noqa: E402
from app.services.database import Database  # noqa: E402

db = Database()
row = (db.select("trading_settings", filters={"id": 1}, order="id", limit=1) or [{}])[0]
print("kill_expand_ttl_min column present :", "kill_expand_ttl_min" in row,
      "->", row.get("kill_expand_ttl_min"))
print("limits now                         :",
      "dd", row.get("max_drawdown_pct"),
      "| daily", row.get("kill_daily_loss_pct"),
      "| weekly", row.get("kill_weekly_loss_pct"),
      "| monthly", row.get("kill_monthly_loss_pct"))
print("kill_expand_requests table ready    :", limit_expand.table_ready(db))
rows = db.select("kill_expand_requests", order="created_at", desc=True, limit=5) or []
print("kill_expand_requests rows (newest 5):", len(rows))
for r in rows:
    print("   ", r.get("status"), r.get("trigger_type"), r.get("metric_value"),
          r.get("requested_at"), "by", r.get("decided_by"))
pause = db.select("trading_pause", filters={"id": 1}, order="id", limit=1)
print("trading_pause                      :", pause)
