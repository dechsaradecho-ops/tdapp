-- 060: meta-filter take-threshold override (was the artifact picked threshold).
-- Mirrors AppSettings.meta_filter_threshold in backend/app/models/schemas.py;
-- execution reads it via G(settings, "meta_filter_threshold"), 0 = artifact threshold.
alter table trading_settings add column if not exists meta_filter_threshold numeric(6,4) not null default 0.5;
