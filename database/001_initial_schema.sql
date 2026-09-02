-- ============================================================
-- AI Wealth & Trading Advisor — Supabase PostgreSQL schema
-- Migration 001: initial schema
-- ============================================================

create extension if not exists "uuid-ossp";

-- ---------- enums ----------
create type user_role as enum ('investor', 'large_investor', 'fund_manager', 'prop_trader', 'admin');
create type risk_profile as enum ('conservative', 'moderate', 'aggressive');
create type trading_mode as enum ('auto', 'semi_auto', 'manual');
create type risk_level as enum ('low', 'medium', 'high');
create type trade_direction as enum ('buy', 'sell');
create type trade_status as enum ('pending_approval', 'open', 'closed', 'cancelled', 'rejected');
create type market_regime as enum (
  'strong_bull_trend', 'bull_trend', 'sideway', 'high_volatility',
  'bear_trend', 'strong_bear_trend', 'news_driven_market'
);
create type notification_channel as enum ('line', 'email', 'in_app');
create type notification_type as enum (
  'new_signal', 'trade_opened', 'trade_closed', 'stop_loss', 'risk_warning',
  'daily_portfolio_summary', 'daily_market_summary', 'weekly_report', 'monthly_report',
  'economic_news', 'semi_auto_approval'
);
create type notification_status as enum ('pending', 'sent', 'failed');
create type approval_status as enum ('pending', 'approved', 'rejected');

-- ---------- users ----------
create table users (
  id            uuid primary key default uuid_generate_v4(),
  email         text not null unique,
  role          user_role not null default 'investor',
  created_at    timestamptz not null default now()
);

-- ---------- portfolios ----------
create table portfolios (
  id             uuid primary key default uuid_generate_v4(),
  user_id        uuid not null references users(id) on delete cascade,
  capital        numeric(18, 2) not null check (capital >= 0),
  target_return  numeric(8, 4) not null,          -- e.g. 3.0 = 3% monthly
  max_drawdown   numeric(8, 4) not null,          -- e.g. 10.0 = 10%
  risk_profile   risk_profile not null default 'moderate',
  trading_mode   trading_mode not null default 'manual',
  currency       text not null default 'THB',
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index idx_portfolios_user on portfolios(user_id);

-- ---------- strategies ----------
create table strategies (
  id          uuid primary key default uuid_generate_v4(),
  name        text not null unique,
  risk_level  risk_level not null,
  description text not null default '',
  created_at  timestamptz not null default now()
);

-- ---------- signals ----------
create table signals (
  id                uuid primary key default uuid_generate_v4(),
  asset             text not null,
  direction         trade_direction not null,
  confidence        numeric(5, 2) not null check (confidence between 0 and 100),
  opportunity_score numeric(5, 2) not null check (opportunity_score between 0 and 100),
  entry             numeric(18, 5),
  stop_loss         numeric(18, 5),
  take_profit       numeric(18, 5),
  expected_rr       numeric(8, 2),
  approval          approval_status not null default 'pending',  -- semi-auto flow
  explanation       text not null default '',
  created_at        timestamptz not null default now()
);
create index idx_signals_asset on signals(asset, created_at desc);
create index idx_signals_pending on signals(approval) where approval = 'pending';

-- ---------- trades ----------
create table trades (
  id           uuid primary key default uuid_generate_v4(),
  user_id      uuid not null references users(id) on delete cascade,
  signal_id    uuid references signals(id) on delete set null,
  asset        text not null,
  direction    trade_direction not null,
  volume       numeric(12, 2) not null default 0,
  entry_price  numeric(18, 5),
  stop_loss    numeric(18, 5),
  take_profit  numeric(18, 5),
  pnl          numeric(18, 2),
  status       trade_status not null default 'open',
  opened_at    timestamptz,
  closed_at    timestamptz,
  created_at   timestamptz not null default now()
);
create index idx_trades_user on trades(user_id, created_at desc);
create index idx_trades_status on trades(status) where status = 'open';

-- ---------- market analysis ----------
create table market_analysis (
  id              uuid primary key default uuid_generate_v4(),
  asset           text not null,
  regime          market_regime not null,
  sentiment       text not null,                   -- bullish / bearish / neutral
  confidence      numeric(5, 2) not null check (confidence between 0 and 100),
  expected_return numeric(8, 4),
  explanation     text not null default '',
  created_at      timestamptz not null default now()
);
create index idx_market_analysis_asset on market_analysis(asset, created_at desc);

-- ---------- ai explanations ----------
create table ai_explanations (
  id          uuid primary key default uuid_generate_v4(),
  trade_id    uuid not null references trades(id) on delete cascade,
  explanation text not null,
  created_at  timestamptz not null default now()
);
create index idx_ai_explanations_trade on ai_explanations(trade_id);

-- ---------- notifications ----------
create table notifications (
  id         uuid primary key default uuid_generate_v4(),
  user_id    uuid references users(id) on delete cascade,
  channel    notification_channel not null default 'line',
  type       notification_type not null,
  message    text not null,
  status     notification_status not null default 'pending',
  sent_at    timestamptz,
  created_at timestamptz not null default now()
);
create index idx_notifications_user on notifications(user_id, created_at desc);
create index idx_notifications_pending on notifications(status) where status = 'pending';

-- ---------- line users ----------
create table line_users (
  id                   uuid primary key default uuid_generate_v4(),
  user_id              uuid not null references users(id) on delete cascade,
  line_user_id         text not null unique,
  notification_enabled boolean not null default true,
  created_at           timestamptz not null default now()
);
create index idx_line_users_user on line_users(user_id);

-- ---------- risk events (limit breaches) ----------
create table risk_events (
  id          uuid primary key default uuid_generate_v4(),
  user_id     uuid not null references users(id) on delete cascade,
  event_type  text not null,                       -- daily_loss_limit / drawdown_limit / ...
  detail      jsonb not null default '{}',
  resolved_at timestamptz,
  created_at  timestamptz not null default now()
);
create index idx_risk_events_user on risk_events(user_id, created_at desc);

-- ---------- updated_at trigger ----------
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger trg_portfolios_updated
  before update on portfolios
  for each row execute function set_updated_at();

-- ---------- Row Level Security (RLS) ----------
alter table portfolios        enable row level security;
alter table trades            enable row level security;
alter table notifications     enable row level security;
alter table line_users        enable row level security;
alter table risk_events       enable row level security;
alter table ai_explanations   enable row level security;

create policy "own portfolio" on portfolios
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own trades" on trades
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own notifications" on notifications
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own line users" on line_users
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own risk events" on risk_events
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- signals / market_analysis / strategies are read-only shared market data
alter table signals         enable row level security;
alter table market_analysis enable row level security;
alter table strategies      enable row level security;

create policy "read signals" on signals for select using (true);
create policy "read market analysis" on market_analysis for select using (true);
create policy "read strategies" on strategies for select using (true);

-- ---------- seed strategies ----------
insert into strategies (name, risk_level, description) values
  ('Trend Following EMA/ADX', 'medium',  'EMA crossover with ADX trend-strength filter and Supertrend trailing.'),
  ('Mean Reversion RSI',      'medium',  'RSI + MACD mean reversion in sideway regimes.'),
  ('Momentum Breakout ATR',   'high',    'ATR-based breakout with volatility sizing.'),
  ('News Swing',              'high',    'Post-event (CPI/NFP/FOMC) directional swing with tight risk.'),
  ('Capital Preservation',    'low',     'Cash-heavy allocation, low exposure, defensive stops.')
on conflict (name) do nothing;
