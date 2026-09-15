// Shared types mirroring backend schemas
export type RiskProfile = "conservative" | "moderate" | "aggressive";
export type TradingMode = "auto" | "semi_auto" | "manual";
export type FinalDecision = "TRADE" | "WAIT" | "REDUCE RISK" | "INCREASE CASH";

export interface Scenario {
  label: "best_case" | "normal_case" | "worst_case";
  expected_return_pct: number;
  expected_profit: number;
  expected_drawdown_pct: number;
  note: string;
}

/** Live portfolio/market state the goal assessment was adjusted by.
 *  Enriched fields (2026-09-11 goal-vs-live sync) mirror the monitor
 *  dashboard — all optional so old responses still parse. */
export interface GoalRealityContext {
  data_available: boolean;
  pnl_total: number;
  win_rate: number;
  closed_count: number;
  open_positions: number;
  market_regime: string;
  market_sentiment: string;
  kill_switch_engaged: boolean;
  kill_triggers: string[];
  trading_paused: boolean;
  pause_reason: string;
  /** Unrealized PnL of open positions at live marks (USD). */
  unrealized_pnl?: number;
  /** Live equity = settings capital + realized + unrealized. */
  equity?: number;
  /** Peak-to-current drawdown % from equity_snapshots. */
  drawdown_pct?: number;
  trades_today?: number;
  trades_week?: number;
  order_mode?: string;
  allowed_assets?: string[];
  risk_per_trade_pct?: number;
  /** trading_settings.capital — the single source of truth. */
  settings_capital?: number;
  /** Asset behind market_regime (top opportunity scorer). */
  top_asset?: string;
  top_score?: number;
}

export interface GoalAssessment {
  capital: number;
  target_return_pct: number;
  expected_profit: number;
  probability: "high_probability" | "moderate_probability" | "low_probability";
  scenarios: Scenario[];
  risk_warning: string | null;
  reasoning: string[];
  /** Present when the backend folded real trading state into the result. */
  reality?: GoalRealityContext | null;
}

export interface AssetOpportunity {
  asset: string;
  score: number;
  band: "low" | "medium" | "high" | "very_high";
  reasons: string[];
  /** Full scoring breakdown (ทุก component) — ใช้ใน popup รายละเอียดคะแนน */
  score_reasons?: string[];
}

export interface MarketSummary {
  regime: string;
  confidence: number;
  explanation: string;
  sentiment: "bullish" | "bearish" | "neutral";
  opportunities: AssetOpportunity[];
  min_confidence?: number;
  min_confidence_gold?: number | null;
}

export interface DbCheckResult {
  table?: string;
  client: "ok" | "unavailable";
  verdict: "pass" | "fail" | "partial";
  insert?: "ok" | "FAIL";
  select?: "ok" | "FAIL";
  delete?: "ok" | "FAIL";
  worker_table?: string;
  worker_insert?: "ok" | "FAIL";
  worker_insert_error?: string;
  worker_insert_hint?: string;
  error?: string;
  insert_hint?: string;
  select_hint?: string;
  token?: string;
}

export interface DbCounts {
  client: "ok" | "unavailable";
  verdict: "ok" | "fail";
  error?: string;
  market_analysis?: number;
  signals?: number;
  news_analysis?: number;
  trades?: number;
  market_analysis_latest?: string;
  signals_latest?: string;
  news_analysis_latest?: string;
  trades_latest?: string;
}

// ---------- Quote API call log (7-day auto-expiry) ----------
export interface QuoteApiLog {
  id: string;
  created_at: string;
  asset: string;
  category: "forex" | "gold";
  provider: string;
  url: string;
  api_key_hint: string | null;
  status: "success" | "error";
  http_status: number | null;
  price: number | null;
  error: string | null;
  duration_ms: number | null;
}

export interface QuoteLogBucket {
  total: number;
  success: number;
  error: number;
}

export interface QuoteLogSummary {
  total: number;
  success: number;
  error: number;
  forex: QuoteLogBucket;
  gold: QuoteLogBucket;
  by_provider: Record<string, QuoteLogBucket>;
}

export interface QuoteLogsResponse {
  client: "ok" | "unavailable";
  verdict: "ok" | "fail";
  error?: string;
  logs: QuoteApiLog[];
  summary: QuoteLogSummary;
  ttl_days: number;
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
}

// ---------- Signal lifecycle log (7-day retention) ----------
export interface SignalLog {
  id: string;
  created_at: string | null;
  signal_id: string | null;
  asset: string;
  direction: string;
  event: "created" | "order_opened" | "order_blocked" | "rejected"
    | "expired" | "sl_moved" | "closed";
  confidence: number | null;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  source: string;
  reason: string;
  ticket: string | null;
  volume: number | null;
  pnl: number | null;
  exit_price: number | null;
}

export interface SignalLogSummary {
  total: number;
  by_event: Record<string, number>;
  by_asset: Record<string, number>;
  opened: number;
  blocked: number;
  expired: number;
  rejected: number;
  /** ย้าย SL ของไม้ที่เปิดอยู่ (breakeven / trailing / ปรับมือ) */
  sl_moved: number;
  closed: number;
}

export interface SignalLogsResponse {
  client: "ok" | "unavailable";
  verdict: "ok" | "fail";
  error?: string;
  logs: SignalLog[];
  summary: SignalLogSummary;
  ttl_days: number;
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
}

export interface QuoteTestResult {
  verdict: "ok" | "fail";
  prices: Record<string, number>;
  failures: Record<string, string>;
  tested_at?: string;
  hint?: string;
  error?: string;
}

// ---------- News analysis history (worker #2 output) ----------
export interface NewsLog {
  id: string;
  created_at: string | null;
  event: string;
  sentiment: number | null;
  affected_assets: string[];
  analysis: string | null;
  confidence: number | null;
}

export interface NewsLogsResponse {
  client: "ok" | "unavailable";
  verdict: "ok" | "fail";
  error?: string;
  logs: NewsLog[];
  summary: {
    total: number;
    by_event: Record<string, number>;
    real: number;
    heuristic: number;
  };
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
}

// ---------- Scheduler run log (7-day auto-expiry) ----------
export interface SchedulerJobStat {
  ticks: number;
  ok: number;
  error: number;
  skipped: number;
  last_started: number | null;
  last_ms: number | null;
  running_s: number | null;
  last_status: "ok" | "error" | "";
  last_detail: string;
  last_error: string;
}

export interface SchedulerLog {
  id: string;
  created_at: string | null;
  job_id: string;
  status: "ok" | "error";
  duration_ms: number | null;
  detail: string;
  error: string;
}

export interface SchedulerLogsResponse {
  client: "ok" | "unavailable";
  verdict: "ok" | "fail";
  error?: string;
  logs: SchedulerLog[];
  summary: {
    total: number;
    ok: number;
    error: number;
    by_job: Record<string, { total: number; ok: number; error: number }>;
  };
  /** heartbeat จาก scheduler ในโปรเซส (ticks/ok/error/skipped/running_s)
   *  — ต่างจาก summary ที่นับ row: อันนี้พิสูจน์ว่า job "ถูกเรียก" จริง
   *  แม้ row จะถูกข้าม/ไม่ถูกเขียน */
  job_stats?: Record<string, SchedulerJobStat>;
  ttl_days: number;
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
}

// ---------- Risk audit trail (risk_events — ไม่มี TTL ไม่ถูกลบ) ----------
export interface RiskEventLog {
  id: string;
  created_at: string | null;
  /** limit_breach | limit_expanded | limit_expand_rejected (หรือค่าอื่นในอนาคต) */
  event_type: string;
  resolved_at: string | null;
  /** รายละเอียดตามชนิดเหตุการณ์ (limit_expanded = {request_id, limit_before,
   *  limit_after, triggers[], approved} · limit_breach = RiskStatus dump) */
  detail: Record<string, unknown>;
}

/** คำขอยืนยันขยายลิมิต (kill_expand_requests) = ต้นทางของการตัดสินใจ */
export interface RiskAuditRequest {
  id: string;
  status: "pending" | "approved" | "rejected" | "expired" | string;
  trigger_type: string;
  metric_value: number | null;
  limit_before: number | null;
  limit_after: number | null;
  requested_at: string | null;
  decided_at: string | null;
  /**
   * "" = ยังไม่ตัดสิน
   * "ui"/"line:..." = เจ้าของกดเอง
   * "auto:expired" = ระบบขยายให้ (นโยบายเปิด — ขยายทุกครั้ง)
   * "auto:once" = ระบบขยายให้เองครั้งเดียวในรอบ 24 ชม. (โควตาขยายอัตโนมัติ)
   * "auto:capped" = โควตาหมด + นโยบายปิด → ไม่ขยาย ปล่อยให้ kill switch ทำงาน
   */
  decided_by: string;
  detail: Record<string, unknown>;
}

export interface RiskLogsResponse {
  client: "ok" | "unavailable";
  verdict: "ok" | "fail";
  error?: string;
  logs: RiskEventLog[];
  requests: RiskAuditRequest[];
  summary: {
    total: number;
    by_event: Record<string, number>;
    /** จำนวนแถวที่สแกนมานับ by_event (เพดานฝั่ง backend) */
    scanned: number;
  };
  /**
   * สถานะการเขียน audit (จากหลักฐานฝั่ง backend ไม่ใช่การเดา):
   * "ok" = มีแถวใน risk_events
   * "empty" = ตารางว่างและ *ไม่* มีการเขียนที่ล้มเหลว → แค่ยังไม่มีเหตุการณ์
   * "write_failed" = write_audit เพิ่ง error จริง (ดู audit_error)
   */
  audit_state?: "ok" | "empty" | "write_failed";
  /** อธิบายว่าทำไมตารางว่าง/เขียนไม่ลง — แสดงในกล่องแจ้งเตือนของแท็บ Audit */
  audit_hint?: string;
  /** error ดิบจาก write_audit ครั้งล่าสุด (มีเมื่อ audit_state = write_failed) */
  audit_error?: string | null;
  /** การตัดสินใจที่อนุมัติแล้วแต่ risk_events ไม่มีแถว (เขียนก่อน 038) */
  audit_missing?: {
    count: number;
    latest: { id: string; status: string; decided_at: string | null; decided_by: string };
  };
  event_types: string[];
  offset: number;
  limit: number;
  total?: number;
  has_more: boolean;
}

export interface MarketSummary {
  regime: string;
  confidence: number;
  explanation: string;
  sentiment: "bullish" | "bearish" | "neutral";
  opportunities: AssetOpportunity[];
}

export interface AllocationItem {
  asset: string;
  weight_pct: number;
  rationale: string;
}

export interface PortfolioRecommendation {
  allocation: AllocationItem[];
  total_weight_pct: number;
  expected_monthly_return_pct: number;
  expected_drawdown_pct: number;
  reasoning: string[];
}

export interface RiskStatus {
  risk_level: "low" | "medium" | "high" | "critical";
  current_drawdown_pct: number;
  max_drawdown_pct: number;
  daily_loss_pct: number;
  weekly_loss_pct: number;
  monthly_loss_pct: number;
  open_risk_pct: number;
  trading_paused: boolean;
  message: string;
  /** Limits the values are judged against (0 = unknown/legacy payload). */
  daily_loss_limit: number;
  weekly_loss_limit: number;
  monthly_loss_limit: number;
  risk_per_trade_pct: number;
  /** Individual breach reasons (joined into message). */
  breaches: string[];
  /** Provenance of open_risk_pct. */
  open_risk_amount: number;
  equity: number;
  open_positions: number;
}

export interface LimitLevel {
  price: number;
  risk_pct: number;
  sl: number;
  tp: number;
  rr: number;
}

// One SL/TP distance tier (สั้น ×1.0 / กลาง ×1.5 / ยาว ×2.0 ATR) previewed on
// the signal card; sl_distance_mode (Settings) picks the tier that opens the order.
export interface SLTPLevel {
  label: string;
  atr_multiple: number;
  stop_loss: number;
  take_profit: number;
  rr: number;
}

// Health of the live-price source — backend reports failures explicitly so
// the UI can warn "ราคาอาจไม่อัปเดต" instead of silently showing stale marks.
export interface QuoteFeedStatus {
  state: "ok" | "error";
  source: string;
  fetched_at: string | null;
  failed_assets: string[];
  message: string;
}

export interface SignalProposal {
  asset: string;
  direction: "BUY" | "SELL";
  confidence: number;
  entry: number;
  stop_loss: number;
  take_profit: number;
  expected_rr: number;
  risk_per_trade_pct: number;
  reason: string[];
  recommendation: FinalDecision;
  limit_levels: LimitLevel[];
  // SL/TP preview 3 ระดับระยะ (สั้น/กลาง/ยาว) — empty on legacy rows
  sltp_levels?: SLTPLevel[] | null;
  // Effective sl_distance_mode from settings — the highlighted tier is the
  // one that will actually be used when the order opens.
  sl_distance_mode?: "short" | "medium" | "long" | null;
  // Present only for DB-backed signals (tier 1) — live/demo tiers omit them.
  // approval === "approved" renders an approval stamp instead of buttons.
  approval?: string | null;
  approved_at?: string | null;
  created_at?: string | null;
  // Set on pending cards that cannot become an order right now because a
  // user limit is hit (open positions / daily / weekly) — the card shows the
  // reason instead of the "ระบบจะยิงออเดอร์เอง" note.
  order_blocked?: string | null;
  // CURRENT market price for this card's asset (intraday spot feed) — lets
  // the card show live price next to entry so a stale entry is obvious.
  live_price?: number | null;
  feed_status?: QuoteFeedStatus | null;
  // Pending-only: นาทีที่เหลือก่อนหมดอายุและระบบเริ่มประเมินใหม่ (TTL 30 นาที)
  expires_min_left?: number | null;
  // Explainability: ขั้นตอนคำนวณทีละขั้น (SL จาก ATR, TP จาก RR, ขนาดไม้,
  // สเปรด) — การ์ดแสดงในบล็อก "วิธีคำนวณ" แบบพับได้
  calc_notes?: string[] | null;
  // ขนาดไม้ (lots) ที่ระบบจะเปิดจริง — คำนวณ read-time ด้วยสูตรเดียวกับ
  // execute_signal (risk_to_lot + min_lot floor บน effective SL)
  suggested_lots?: number | null;
}

// ---------- Extended Trading System ----------
export interface FrequencyDecision {
  allowed: boolean;
  reason: string;
  trades_today: number;
  trades_this_week: number;
  open_positions: number;
  limits: {
    max_trades_daily: number;
    max_trades_weekly: number;
    max_open_positions: number;
    risk_per_trade_pct: number;
  } | null;
}

export interface EntryLeg {
  order_type: "market" | "buy_limit" | "sell_limit" | "buy_stop" | "sell_stop";
  price: number;
  lot: number;
  risk_pct: number;
  note: string;
}

export interface OrderPlan {
  asset: string;
  direction: "BUY" | "SELL";
  entries: EntryLeg[];
  total_lots: number;
  total_risk_pct: number;
  average_entry: number;
  stop_loss: number;
  take_profit: number;
  rationale: string[];
}

export interface CorrelationResponse {
  assets: string[];
  portfolio_correlation: number;
  exposure: { currency: string; exposure_pct: number; direction_net: string }[];
  /** เพดานที่ execution gate 4 ใช้ตัดสิน (trading_settings.correlation_cap) */
  correlation_cap?: number;
  /** ไม้ที่เปิดอยู่จริง — ใช้โชว์ในแถบสรุป Opportunity Score */
  open_positions?: { asset: string; direction: string; volume: number }[];
  /** ต่อสัญลักษณ์ — มีเฉพาะเมื่อเรียกด้วย ?assets=A,B,C */
  symbol_risk?: Record<string, CorrelationSymbolRisk>;
}

/** ไม้เปิดที่ซ้ำความเสี่ยงกับสัญลักษณ์ที่กำลังพิจารณา */
export interface CorrelationLinkedPosition {
  asset: string;
  direction: string;
  /** + = ทับความเสี่ยง (ไปทางเดียวกัน), − = สวนทาง (ช่วยกระจาย) */
  correlation: number;
  /** สกุลเงินที่ถือร่วมกัน เช่น ["EUR"] (ว่างเมื่อไม่ใช่คู่ FX) */
  shared: string[];
}

/** ผลประเมินความเสี่ยง correlation ของการเปิดสัญลักษณ์หนึ่งเพิ่ม */
export interface CorrelationSymbolRisk {
  /** high = เกินเพดานจริง (gate บล็อก), none = ไม่ซ้ำกับไม้ไหนเลย */
  level: "none" | "low" | "medium" | "high";
  /** portfolio_correlation ถ้าเปิดคู่นี้เพิ่ม (ตัวเลขเดียวกับที่ gate ใช้) */
  projected: number;
  /** portfolio_correlation ของไม้เปิดปัจจุบัน */
  current: number;
  delta: number;
  over_cap: boolean;
  /** มีไม้เปิดคู่นี้อยู่แล้ว — ระบบกันไม้ซ้ำ */
  duplicate: boolean;
  with: CorrelationLinkedPosition[];
  hedges: CorrelationLinkedPosition[];
}

export interface NewsRisk {
  status: "SAFE" | "CAUTION" | "DANGER";
  reason: string;
  minutes_to_next: number | null;
  /** ข่าว high-impact ตัวถัดไป (backend ส่งมาอยู่แล้ว — เดิม type ไม่ได้ประกาศ) */
  next_high_impact?: {
    event: string;
    currency: string;
    time_utc: string;
    impact: string;
  } | null;
}

/**
 * ตัวอย่าง gate ก่อนเปิดไม้ ระดับพอร์ต — GET /api/trading/gate-preview
 *
 * รูปร่างตรงกับ backend (app/api/routes/trading.py :: get_gate_preview) ทุกฟิลด์
 * ⚠️ อ่าน semantics ให้ดีก่อนเอาไปตัดสินว่าบล็อกหรือไม่:
 * - spread  = วัดจาก "ไม้ที่เปิดอยู่" (proxy, ไม่มี blocking) — gate จริงวัดตอนเปิดไม้ใหม่
 * - currency = ความเสี่ยง ณ จุด SL ของไม้ที่เปิดอยู่ "เท่านั้น" = ค่าต่ำสุด (lower bound)
 * - session  = เป็นด่านระดับพอร์ตจริง → blocking เชื่อถือได้
 * - cooldown = รายสัญลักษณ์ → active = "มีบางคู่ติด" ไม่ได้แปลว่าไม้ถัดไปจะโดน
 */
export interface GatePreview {
  spread: {
    cap_pct: number;
    enabled: boolean;
    /** true = ตัวเลขนี้มาจากไม้ที่เปิดอยู่ ไม่ใช่ไม้ที่กำลังจะเปิด */
    proxy: boolean;
    worst_asset: string | null;
    worst_pct: number | null;
    worst_spread: number | null;
    worst_sl_distance: number | null;
    positions: { asset: string; spread: number; sl_distance: number; spread_pct: number }[];
  };
  pre_news: {
    flatten_min: number;
    enabled: boolean;
    minutes_to_next: number | null;
    event: string | null;
    currency: string | null;
    /** สินทรัพย์ในพอร์ตที่ได้/เสียสกุลของข่าวลูกถัดไป */
    affected_assets: string[];
    /** อยู่ในหน้าต่างงดเปิดไม้ก่อนข่าวแล้ว */
    in_window: boolean;
    blocking: boolean;
  };
  session: {
    enabled: boolean;
    market_closed: boolean;
    overlapping: boolean;
    volatility_hint: string;
    active_sessions: string[];
    blocking: boolean;
    /** ข้อความไทยจาก helper ตัวเดียวกับที่ gate ใช้ — ว่าง = ไม่บล็อก */
    reason: string;
  };
  currency: {
    cap_pct: number;
    enabled: boolean;
    currency: string | null;
    direction: string | null;
    risk_usd: number;
    pct: number;
    over_cap: boolean;
    buckets: { currency: string; direction: string; risk_usd: number; pct: number }[];
  };
  cooldown: {
    minutes: number;
    enabled: boolean;
    active: { asset: string; reason: string }[];
  };
  generated_at: string;
}

export interface SessionStatus {
  active_sessions: string[];
  overlapping: boolean;
  volatility_hint: "low" | "medium" | "high";
  current_utc_time: string;
  /** True during the weekend close (Fri 21:00 UTC → Sun 21:00 UTC). */
  market_closed?: boolean;
  /** ISO timestamp of the next reopen (present only when market_closed). */
  next_open_utc?: string | null;
}

export interface KillSwitch {
  engaged: boolean;
  triggers: string[];
  checked: string[];
  message: string;
}

/** ลิมิตความเสี่ยง 1 ตัวที่ "เกินอยู่" — ตัวเลขที่คำขอยืนยันอ้างอิง */
export interface LimitExpandTrigger {
  trigger: "drawdown" | "daily" | "weekly" | "monthly" | string;
  /** คอลัมน์ใน trading_settings ที่จะถูกเขียนเมื่ออนุมัติ */
  field: string;
  label: string;
  /** ค่าที่วัดได้จริงตอนนี้ (%) */
  value: number;
  /** ลิมิตที่ใช้อยู่ตอนนี้ (%) */
  limit: number;
  /** ลิมิตที่เสนอหลังอนุมัติ (limit + step) (%) */
  new_limit: number;
}

export interface LimitExpandRequestInfo {
  id: string;
  status: "pending" | "approved" | "rejected" | "expired" | string;
  trigger_type: string;
  metric_value: number | null;
  limit_before: number | null;
  limit_after: number | null;
  requested_at: string | null;
  /** requested_at + kill_expand_ttl_min (ค่า default 180 นาที, ตั้งได้ในหน้า
   *  Settings) — เลยเวลาแล้วระบบตัดสินให้ตามนโยบาย kill_expand_auto_apply
   *  (ขยายให้เองทุกครั้ง / ครั้งเดียวใน 24 ชม. / ไม่ขยาย → kill switch ปิดไม้)
   *  คำขอที่ไม่มีลิมิตค้างอยู่จะถูกปิดเป็น expired โดยไม่ขยาย */
  expires_at: string | null;
  age_min: number | null;
  decided_at: string | null;
  /**
   * "ui" / "line:..." = เจ้าของกดเอง
   * "auto:expired" = ระบบขยายให้เองเมื่อพ้นเวลา (นโยบายเปิด — ทุกครั้ง)
   * "auto:once" = ขยายให้เองได้ครั้งเดียวในรอบ 24 ชม.
   * "auto:capped" = โควตาหมด ระบบไม่ขยายให้อีก
   */
  decided_by: string;
}

/**
 * GET /api/trading/limit-expand — สถานะสำหรับ popup ยืนยันขยายลิมิต
 * ใช้ "เงื่อนไขเดียวกับ LINE": popup ขึ้นเมื่อมีคำขอสถานะ pending รอเจ้าของบัญชี
 * เท่านั้น (แถวเดียวกับที่สร้างข้อความ LINE) — ตัวเลขที่โชว์คือตัวเลขของคำขอนั้น
 */
export interface LimitExpandState {
  /** true = มีคำขอรอยืนยันอยู่ → popup ต้องขึ้น */
  pending: boolean;
  /**
   * true = คำขอนี้เลยช่วงยืนยัน (ttl_min) ไปแล้ว แต่ระบบยัง “ตัดสินให้ไม่ได้”
   * (เขียนค่าลิมิตไม่สำเร็จ หรือโควตาขยายอัตโนมัติถูกใช้ไปแล้ว) → คำขอยังเปิดอยู่
   * คำตอบยังมีผล · ปกติระบบจะขยาย/ปิดแถวให้เองภายในรอบถัดไปของ worker
   */
  lapsed: boolean;
  breach: boolean;
  triggers: LimitExpandTrigger[];
  request: LimitExpandRequestInfo | null;
  /** คำขอล่าสุดที่ตัดสินใจไปแล้ว (ไม่นับอันที่ยัง pending) */
  last: LimitExpandRequestInfo | null;
  paused: boolean;
  pause_reason: string;
  /** ยังไม่ได้รัน migration 036 → เก็บคำขอไม่ได้ (LINE แจ้งเหมือนกัน) */
  setup_required: boolean;
  step_pct: number;
  ttl_min: number;
  approve_command: string;
  reject_command: string;
  kill_engaged: boolean;
  kill_triggers: string[];
  /** true = นโยบายเปิด: พ้นเวลาแล้วขยายลิมิตให้อัตโนมัติทุกครั้ง (Settings →
   *  kill_expand_auto_apply) */
  auto_apply: boolean;
  /** true = นโยบายปิด: ระบบขยายให้เองได้ “ครั้งเดียวใน 24 ชม.” ต่อคำขอหนึ่งใบ */
  auto_apply_once: boolean;
  /** true = นโยบายปิด + มีคำขอค้าง → พ้นเวลาแล้วระบบอาจ “ไม่ขยาย” และ
   *  kill switch ปิดไม้ทันที (แจ้งเตือนก่อนปิด) */
  lapsed_closes: boolean;
}

export interface LimitExpandDecisionResult {
  ok: boolean;
  /**
   * "" เมื่อไม่มีคำขอค้าง — การกดอนุมัติลอย ๆ ต้องไม่ขยายลิมิตใด ๆ
   * "auto" เมื่อคำขอหมดเวลายืนยันไปแล้ว — ระบบขยายลิมิตให้เองตามนโยบาย
   * (reply คือผลที่เกิดขึ้นจริง ไม่ใช่ผลจากการกดปุ่มครั้งนี้)
   */
  applied_decision: "approve" | "reject" | "auto" | "";
  /** ข้อความรายงานผลชุดเดียวกับที่ push เข้า LINE */
  reply: string;
  state: LimitExpandState;
}

export interface JournalEntry {
  id?: string | null;
  asset: string;
  direction: "BUY" | "SELL";
  entry_price: number;
  exit_price: number | null;
  holding_time_min: number | null;
  pnl: number | null;
  rr_ratio: number | null;
  market_regime: string;
  opportunity_score: number;
  ai_explanation: string;
  closed_at: string | null;
  created_at: string | null;
}

export interface JournalAnalysis {
  period_days: number;
  total_trades: number;
  win_rate_pct: number;
  profit_factor: number;
  average_rr: number;
  /** Best/worst closed trade in the window (null when no closed trades). */
  best_setup: JournalEntry | null;
  worst_setup: JournalEntry | null;
}

export interface PaperTrading {
  enabled: boolean;
  virtual_capital: number;
  virtual_pnl: number;
  open_virtual_orders: number;
  ai_coaching: string;
  live_readiness_score: number;
  /** false = ยังปิดไม่ครบ 30 ไม้ → live_readiness_score ถูกระงับ (0) */
  readiness_ready: boolean;
  /** จำนวนไม้ที่ปิดแล้วซึ่งใช้คำนวณคะแนน */
  readiness_sample: number;
  readiness_min_sample: number;
}

export interface BacktestConfig {
  asset: string;
  indicator: "EMA" | "RSI" | "MACD" | "ADX" | "ATR" | "SuperTrend" | "PriceAction";
  days: number;
  initial_capital: number;
  risk_per_trade_pct: number;
}

export interface BacktestResult {
  config: BacktestConfig;
  total_trades: number;
  win_rate_pct: number;
  profit_factor: number;
  sharpe_ratio: number;
  max_drawdown_pct: number;
  final_equity: number;
  note: string;
}

export interface WalkForwardResult {
  segments: number;
  in_sample_win_rates: number[];
  out_sample_win_rates: number[];
  reliability_score: number;
  note?: string;
}

export interface ExtendedAnalysis {
  /** สัญลักษณ์ที่ถูกประเมิน (top scorer หรือตัวที่เลือกเอง) */
  asset?: string;
  /** "selected" = ผู้ใช้เลือกราก dropdown | "top_scorer" = ระบบเลือกให้ */
  asset_source?: "selected" | "top_scorer";
  /** ทุกสัญลักษณ์ที่รอบสแกนล่าสุดเห็น + คะแนน/regime (ให้ dropdown ติดคะแนน) */
  universe?: { asset: string; confidence: number; regime: string }[];
  confidence?: number;
  direction?: string;
  regime?: string;
  news_calendar: string;
  session_analysis: string;
  correlation_analysis: string;
  order_strategy: string;
  execution_plan: string;
  risk_officer_review: string;
  journal_insight: string;
  backtest_result: string;
  paper_trading_status: string;
  kill_switch_status: string;
  final_decision: string;
  context_block?: string;
}

/** Response of POST /api/trading/extended-open (open first market leg). */
export interface ExtendedOpenResult {
  ok: boolean;
  status: string;
  final_decision?: string;
  asset?: string;
  direction?: string;
  ticket?: string;
  volume?: number;
  checks?: string[];
  rejects?: string[];
  warnings?: string[];
  remaining_legs?: number;
  message: string;
}

// ---------- App Settings ----------
export interface AppSettings {
  risk_profile: RiskProfile;
  capital: number;
  min_confidence: number;
  /** Gold (XAUUSD) override — null/undefined = use min_confidence */
  min_confidence_gold: number | null;
  min_opportunity: number;
  max_trades_daily: number;
  max_trades_weekly: number;
  max_open_positions: number;
  risk_per_trade_pct: number;
  /** Re-entry cooldown (นาที) หลังปิดไม้คู่ไหนก่อนเปิดคู่นั้นใหม่ — 0 = ปิดฟีเจอร์ */
  reentry_cooldown_min: number;
  /** Minimum lot size for opened orders (floor of risk_to_lot sizing) */
  min_lot: number;
  /** Gold (XAUUSD) override — null/undefined = use min_lot */
  min_lot_gold: number | null;
  /** Move SL to entry once profit ≥ breakeven_trigger_r × R (0 = off) */
  breakeven_trigger_r: number;
  /** Trailing stop distance in ATR multiples (0 = off) */
  trail_atr_mult: number;
  /** Close this % of volume at partial_trigger_r × R (0 = off) */
  partial_close_pct: number;
  /** R-multiple that triggers the partial close */
  partial_trigger_r: number;
  /** Time stop: close positions older than this many days (0 = off) */
  max_hold_days: number;
  /** Reward:Risk target — TP = SL distance × rr_target (1:2 default) */
  rr_target: number;
  /** Smart Exit Engine — continuous AI exit evaluation (false = legacy guard only) */
  smart_exit_enabled: boolean;
  /** Hold-quality score below which the engine closes/scales out */
  exit_score_close: number;
  /** Profit ≥ this R with quality != High → scale out 50% (0 = off) */
  profit_protect_r: number;
  /** Opportunity score below this counts as a reversal vote */
  reversal_opp_min: number;
  /** News Exit master switch (DANGER + profit → close early) */
  news_exit_enabled: boolean;
  news_exit_min_r: number;
  /** ATR% above this + profit → scale out (0 = off) */
  volatility_exit_atr: number;
  /** NO POSITION LEFT BEHIND: profit < this R + age > threshold → close.
   *  ไม้นี้คือไม้ตายหลักสำหรับปิดไม้ตามเวลา (ยิงก่อน max_hold_days) */
  no_behind_min_r: number;
  /** threshold = mult × ค่าเฉลี่ยเวลาถือ (0 = ปิดกฎนี้) */
  no_behind_hold_mult: number;
  /** พื้นขั้นต่ำของ threshold (วัน) — กันค่าเฉลี่ยพังแล้วเกณฑ์สั้นเกินไป (0 = ไม่มีพื้น) */
  no_behind_min_days: number;
  /** Time stop ยกเว้นไม้กำไร: ไม้แก่เกิน max_hold_days แต่กำไร ≥ ค่านี้จะไม่ถูกปิด (0 = ปิดตามอายุเสมอ) */
  time_stop_min_r: number;
  /** R-ladder trailing 1R→BE / 2R→+1R / 3R→+2R (false = legacy breakeven+trail) */
  trailing_ladder: boolean;
  /** Strategy D: XAUUSD only trades breakout/retest setups (false = old behaviour) */
  gold_breakout_only: boolean;
  /** Simulated spread (price units) applied to paper fills — legacy global
   *  fallback; per-symbol spreads take precedence for known assets */
  paper_spread: number;
  /** Per-symbol spread overrides (asset → spread in price units) — null =
   *  built-in realistic defaults; symbols without an entry use defaults */
  spread_overrides: Record<string, number> | null;
  /** Exit-side spread multiplier charged on closing a paper trade (migration
   *  033) — half the entry spread by default (spread is typically quoted
   *  once), 0 = charge no exit spread (pre-2026-09-11 behaviour) */
  paper_exit_spread_mult: number;
  /** Round-turn commission per lot charged to paper PnL (USD/lot; 0 = off) */
  paper_commission_per_lot: number;
  max_drawdown_pct: number;
  kill_daily_loss_pct: number;
  kill_weekly_loss_pct: number;
  kill_monthly_loss_pct: number;
  drawdown_throttle_pct: number;
  /** นาทีที่รอการยืนยันขยายลิมิตความเสี่ยง (LINE + popup) ก่อนคำขอหมดอายุ —
   *  default 180; เกินเวลาแล้วระบบตัดสินให้ตามนโยบายด้านล่าง (migration 037) */
  kill_expand_ttl_min: number;
  /** นโยบายเมื่อคำขอหมดเวลายืนยัน (migration 039) —
   *  true (default) = ขยายลิมิตให้อัตโนมัติทุกครั้ง,
   *  false = ขยายให้เองได้ครั้งเดียวใน 24 ชม. หลังจากนั้น “ไม่ขยาย” และ
   *  ปล่อยให้ kill switch ปิดไม้ตามลิมิตเดิม */
  kill_expand_auto_apply: boolean;
  news_block_minutes: number;
  news_caution_minutes: number;
  correlation_cap: number;
  /** เพดานความเสี่ยงต่อสกุล (%) — Gate 4b (migration 041): รวม risk-at-stop
   *  ต่อสกุล+ทิศทาง (ไม้เปิด + ไม้นี้) ห้ามเกิน % ของทุน — 0 = ปิด (default 50) */
  max_currency_exposure_pct: number;
  /** เพดานสเปรดต่อระยะ SL (%) — Gate 3b (migration 041): สเปรดกินระยะ SL
   *  เกิน % นี้ = edge หาย งดเปิดไม้ — 0 = ปิด (default 25) */
  spread_guard_max_pct: number;
  /** งดเปิดไม้ใหม่ก่อนข่าว high-impact กี่นาที — Gate 3b (migration 041):
   *  เฉพาะข่าวของสกุลในคู่นั้น — 0 = ปิด (default 30) */
  pre_news_flatten_min: number;
  /** ตัวกรอง session — Gate 3b (migration 041): งดเปิดไม้ใหม่ตอนตลาดปิด
   *  (weekend) หรือสภาพคล่องต่ำ (Sydney-only) — false = ปิด (default true) */
  session_filter_enabled: boolean;
  order_mode: string;
  sl_distance_mode: "short" | "medium" | "long";
  /** SL distance clamp (% of price) — 0 disables a bound; min=max forces a fixed distance */
  sl_distance_min_pct: number;
  sl_distance_max_pct: number;
  /** SL risk cap — tighten a wide SL so the floor-sized order never risks
   *  over the per-trade budget (false = keep the structural SL) */
  sl_cap_enabled: boolean;
  default_equity: number;
  paper_virtual_capital: number;
  backtest_days: number;
  backtest_indicator: string;
  backtest_asset: string;
  /** Auto-refresh interval (seconds) for the monitor page — 0 = off (DB-stored) */
  monitor_refresh_sec: number;
  /** Auto-refresh interval (seconds) for the signals page — 0 = off (DB-stored) */
  signals_refresh_sec: number;
  /** LINE notification categories — per-category on/off (default true) */
  notify_trade_opened: boolean;
  notify_trade_closed: boolean;
  notify_stop_loss: boolean;
  notify_risk_warning: boolean;
  notify_daily_digest: boolean;
  notify_daily_summary: boolean;
  /** Tradable universe — scanner analyses + platform trades these pairs only */
  allowed_assets: string[];
  /** AI chat model name (Settings page) — "" = use backend/ai.config.json.
   *  Applies to the chat widget, LINE webhook and journal explanations; takes
   *  effect immediately after save (no redeploy). */
  ai_model: string;
  /** OpenAI-compatible base URL (e.g. https://api.deepseek.com) — "" = use
   *  ai.config.json. /chat/completions is appended by the backend. */
  ai_base_url: string;
}

/** Pairs the price feeds cover (mirror of backend quotes.SUPPORTED_ASSETS).
 *  Only these are offered in the Settings add-pair dropdown. */
export const SUPPORTED_ASSETS = [
  "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF",
  "EURGBP", "EURJPY", "EURAUD", "EURNZD", "EURCAD", "EURCHF",
  "GBPJPY", "GBPAUD", "GBPNZD", "GBPCAD", "GBPCHF",
  "AUDJPY", "AUDNZD", "AUDCAD", "AUDCHF", "NZDJPY", "NZDCAD",
  "CADJPY", "CADCHF", "CHFJPY",
  "XAUUSD",
] as const;

export const DEFAULT_ASSETS: string[] = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"];

export interface SettingsSaveResult {
  ok: boolean;
  settings: AppSettings;
  message: string;
}

/** Response of POST /api/ai/test — the Settings page "ทดสอบการเชื่อมต่อ"
 *  button. 200 even on failure: `ok:false` + `error` carry the reason so it
 *  can be shown inline instead of a toast. `provider`/`model`/`base_url` are
 *  the values the backend ACTUALLY used for the round trip (so a wrong model
 *  name or a stale URL is visible at a glance). */
export interface AITestResult {
  ok: boolean;
  provider: string;
  model?: string;
  base_url?: string;
  /** Short reply text from the model (truncated) — proof it really answered */
  reply?: string;
  error?: string;
  /** Effective (non-secret) AI config snapshot from the backend */
  info?: Record<string, string>;
}

/** Daily OHLC candle from GET /api/market/candles (oldest-first). */
export interface MarketCandle {
  o: number;
  h: number;
  l: number;
  c: number;
}

/** Response of GET /api/market/candles?asset=&days= — fail-soft: feed
 *  failure → {candles: [], error} with 200 so the popup still shows
 *  entry/SL/TP + position details without a chart. */
export interface MarketCandlesResponse {
  asset: string;
  candles: MarketCandle[];
  count: number;
  error: string;
}

/** Shared trading kill-switch state (read by auto trader + /approve). */
export interface PauseStatus {
  paused: boolean;
  reason: string;
  paused_at: string | null;
}

/** 9-factor Smart Exit breakdown (0-100 each, higher = safer to hold). */
export interface SmartExitFactorScores {
  trend_strength: number;
  momentum: number;
  volume_proxy: number;
  market_regime: number;
  news_risk: number;
  holding_time: number;
  volatility: number;
  opportunity_score: number;
  risk_exposure: number;
}

export interface SmartExitSignals {
  tp_hit: boolean;
  sl_hit: boolean;
  trailing: boolean;
  reversal: boolean;
  news: boolean;
  time_stop: boolean;
}

/** Per-position Smart Exit analysis (null when disabled/unevaluated). */
export interface SmartExitInfo {
  position_age_days: number;
  r_multiple: number;
  exit_score: number;
  quality: string;
  factors: SmartExitFactorScores;
  signals: SmartExitSignals;
  recommendation: string;
  final: string;
  reasoning: string[];
  trigger: string;
}

/** One open paper position with a live mark and unrealized PnL. */
export interface MonitorOpenPosition {
  id: string;
  ticket: string;
  asset: string;
  direction: string;
  volume: number;
  entry_price: number;
  stop_loss: number | null;
  take_profit: number | null;
  current_price: number;
  unrealized_pnl: number;
  source: string;
  created_at: string | null;
  /** SL/TP move tracking (migration 021) — badge + tooltip on monitor. */
  initial_stop_loss: number | null;
  initial_take_profit: number | null;
  sl_moved_at: string | null;
  sl_move_reason: string;
  tp_moved_at: string | null;
  tp_move_reason: string;
  exit_info?: SmartExitInfo | null;
  // Explainability: R ปัจจุบัน, $ เสี่ยงถ้าโดน SL, ที่มาราคา
  // (spot/daily/broker/entry), ขั้นตอนคำนวณทีละขั้น — ตารางมอนิเตอร์แสดง
  r_multiple?: number | null;
  risk_amount?: number | null;
  price_source?: string | null;
  calc_notes?: string[] | null;
}

/** One execution-journal row (open, closed or rejected). */
export interface MonitorTrade {
  id: string;
  asset: string;
  direction: string;
  volume: number;
  entry_price: number;
  exit_price: number | null;
  pnl: number | null;
  status: string;
  source: string;
  ticket: string | null;
  close_reason: string | null;
  closed_at: string | null;
  created_at: string | null;
  /** Realized created_at → closed_at span; null for rows closed before
   *  the close reason was tracked (no backfill — never fabricate history). */
  holding_days?: number | null;
  stop_loss?: number | null;
  initial_stop_loss?: number | null;
}

/** Live exit-rule thresholds — the numbers the close-reason popup explains. */
export interface MonitorExitRules {
  smart_exit_enabled: boolean;
  max_hold_days: number;
  avg_hold_days: number;
  left_behind_days: number;
  no_behind_hold_mult: number;
  no_behind_min_r: number;
  no_behind_min_days: number;
  time_stop_min_r: number;
  exit_score_close: number;
}

export interface MonitorStats {
  trades_today: number;
  trades_week: number;
  open_positions: number;
  closed_count: number;
  win_rate: number;
  pnl_today: number;
  pnl_week: number;
  pnl_total: number;
}

export interface MonitorSnapshot {
  pause: PauseStatus;
  order_mode: string;
  capital: number;
  kill: KillSwitch;
  stats: MonitorStats;
  open_positions: MonitorOpenPosition[];
  recent: MonitorTrade[];
  generated_at: string | null;
  feed_status?: QuoteFeedStatus | null;
  /** Live portfolio value from DB — capital + realized + unrealized PnL */
  equity: number;
  pnl: number;
  /** Real Risk Engine status computed server-side (same inputs as the worker). */
  risk?: RiskStatus | null;
  /** Live exit-rule thresholds for the close-reason popup. */
  exit_rules?: MonitorExitRules | null;
}

/** Response of POST /api/trading/positions/close (manual close popup). */
export interface ClosePositionResult {
  ok: boolean;
  ticket: string;
  asset: string;
  direction: string;
  volume: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
  holding_time_min: number | null;
  close_reason: string;
  message: string;
  remaining_open: number;
  total_realized_pnl: number;
  pnl_today: number;
  wins: number;
  losses: number;
  trade_id: string;
  warnings: string[];
}

/** Response of POST /api/trading/stats/reset (🗑 รีเซ็ตสถิติ on monitor). */
export interface StatsResetResult {
  ok: boolean;
  deleted: number;
  message: string;
  /** Fresh stats after the reset (same shape as MonitorStats). */
  stats: MonitorStats;
  warnings: string[];
}

/** One point of the equity curve (GET /api/trading/equity-curve). */
export interface EquityPoint {
  date: string;
  equity: number;
}

/** Response of GET /api/trading/equity-curve (performance page chart). */
export interface EquityCurve {
  points: EquityPoint[];
  latest_equity: number;
  peak_equity: number;
  drawdown_pct: number;
  capital: number;
  /** true when no snapshots exist yet (flat line at settings capital) */
  synthetic: boolean;
}

/** Per-ticket result inside POST /api/trading/positions/close-all. */
export interface CloseAllItem {
  ticket: string;
  asset: string;
  ok: boolean;
  pnl?: number;
  exit_price?: number;
  message?: string;
}

/** Response of POST /api/trading/positions/close-all (ปิดทั้งหมด button). */
export interface CloseAllResult {
  ok: boolean;
  closed: number;
  failed: number;
  total_pnl: number;
  results: CloseAllItem[];
  message: string;
}

/** One aggregated row of GET /api/trading/signal-report. */
export interface SignalReportRow {
  key: string;
  trades: number;
  win_rate_pct: number;
  total_pnl: number;
}

/** Response of GET /api/trading/signal-report (signal quality feedback). */
export interface SignalReport {
  days: number;
  signals: number;
  matched_trades: number;
  by_asset: SignalReportRow[];
  by_confidence_band: SignalReportRow[];
  by_regime: SignalReportRow[];
}

// ---------- Auth: 6-digit PIN gate ----------
export interface PinStatus {
  pin_set: boolean;
  locked: boolean;
  locked_until: string | null;
  failed_attempts: number;
  max_failed: number;
  lock_minutes: number;
}

// ---------- LINE: notification targets + test button ----------
/** One registered chat from GET /api/line/targets (group/room auto-registered
 * by the webhook, or a personal line_users row). */
export interface LineTarget {
  target_id: string;      // groupId / roomId / userId (C... / U...)
  target_type: string;    // group | room | user
  notification_enabled: boolean;
  created_at?: string | null;
  last_seen_at?: string | null;  // latest webhook event received from this chat
}

/** Response of GET /api/line/targets. */
export interface LineTargetsResponse {
  targets: LineTarget[];
  users: LineTarget[];
}

/** Per-target result of POST /api/line/test. */
export interface LineTestItem {
  target_id: string;
  target_type: string;
  ok: boolean;
  error?: string;   // raw LINE API error when ok=false
}

/** Response of POST /api/line/test (🔔 ทดสอบ button). */
export interface LineTestResult {
  ok: boolean;
  sent: number;
  failed: number;
  results: LineTestItem[];
  hint: string;
}

/** Response of POST /api/line/targets (manual groupId add). */
export interface LineTargetMutation {
  ok: boolean;
  message: string;
  target?: LineTarget;
}

/** Response of GET /api/line/diag (one-shot setup diagnosis). */
export interface LineDiag {
  token_set: boolean;
  secret_set: boolean;
  bot_user_id_set: boolean;
  bot_user_id: string;
  db_available: boolean;
  targets_table_ok: boolean;
  targets_count: number;
  users_count: number;
  table_error?: string;
  token_valid?: boolean;
  bot_user_id_from_api?: string;
  display_name?: string;
  hint?: string;
}

/** One webhook activity record from GET /api/line/events. */
export interface LineEvent {
  at: string;
  kind: string;   // received | signature_rejected | skipped_* | replied | simulated
  [key: string]: unknown;
}

/** Response of GET /api/line/events (webhook debug log). */
export interface LineEventsResponse {
  events: LineEvent[];
}

/** One pipeline step of POST /api/line/simulate. */
export interface SimStep {
  step: string;
  ok: boolean;
  note?: string;
  via?: string;
  reply?: string;
}

/** Response of POST /api/line/simulate (debug: what would the bot reply?). */
export interface LineSimulateResult {
  ok: boolean;
  reply: string | null;
  via?: string;
  steps: SimStep[];
  note?: string;
  pushed?: boolean;
  push_error?: string;
}

export interface PinLoginResponse {
  ok: boolean;
  token: string | null;
  message: string;
  remaining_attempts: number | null;
  locked_until: string | null;
}

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
