-- 044_entry_management_modes.sql
-- P1-2: split the legacy order_mode (auto / semi_auto / manual) into two
-- INDEPENDENT axes. The single field conflated two orthogonal decisions:
--
--   * who OPENS a position (signal → order pipeline), and
--   * who MANAGES an open position (breakeven / trail / partial / smart-exit
--     / time stop).
--
-- With one enum it was impossible to say e.g. "every fill needs a human
-- Approve (confirm) BUT the guard should still protect the book (auto)".
--
-- New axes (both default '' = "derive from order_mode", so an old row is
-- byte-identical until the owner sets them explicitly — see
-- AppSettings.effective_entry_mode / effective_position_management_mode):
--
--   entry_mode:               'auto' | 'confirm' | 'advisory'
--       auto     — the auto-trader may open without a human
--       confirm  — every order needs an explicit Approve (legacy semi_auto)
--       advisory — no auto opening; signals are informational only
--
--   position_management_mode: 'auto' | 'protective_only' | 'advisory'
--       auto            — full guard management (partial/BE/trail/ladder/
--                         smart-exit/time-stop) PLUS Hard SL/TP + Emergency
--       protective_only — Hard SL/TP + Emergency + BE/Trail/R-ladder, but NO
--                         partial close / smart-exit / reversal / time stop
--       advisory        — NO discretionary auto-management. Hard SL/TP +
--                         Emergency Exit REMAIN a safety INVARIANT (the guard
--                         never silently drops hard-stop protection).
--
-- Legacy derivation mapping (when the new column is ''):
--   order_mode auto      → entry auto            / management auto
--   order_mode semi_auto → entry confirm         / management protective_only
--   order_mode manual    → entry advisory        / management advisory
--
-- Empty-string DEFAULT (NOT null) so the pre-P1-2 behaviour is the default
-- and no backfill is required.

alter table trading_settings
  add column if not exists entry_mode text not null default '';

alter table trading_settings
  add column if not exists position_management_mode text not null default '';

comment on column trading_settings.entry_mode is
  'P1-2 who may OPEN: auto | confirm | advisory. Empty string (default) => derive from the legacy order_mode column.';

comment on column trading_settings.position_management_mode is
  'P1-2 who MANAGES an open position: auto | protective_only | advisory. Empty string (default) => derive from the legacy order_mode column. advisory still keeps Hard SL/TP + Emergency Exit as a safety invariant.';
