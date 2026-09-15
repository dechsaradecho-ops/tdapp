# tdapp — สรุปฟีเจอร์ทั้งหมด (FEATURES)

> อัปเดตล่าสุด: 2026-09-15 · commit `75a222e` (monitor: recent แถวปิดโชว์ `closed_at` + คอลัมน์ Ticket) · ก่อนหน้า `4084011` (re-entry cooldown Gate 2b)
> เอกสารเทคนิคอื่น: `README.md` (quick start), `UI-DESIGN-SYSTEM.md` (ดีไซน์), `MENU-REORG-PLAN.md` (โครงเมนู 5 เมนู), `database/*.sql` (migration 001–040)

## 1. ภาพรวมระบบ

แพลตฟอร์มให้คำแนะนำการเทรดหลายสินทรัพย์ (Forex, ทอง XAUUSD, Crypto, ดัชนี, CFD) — วิเคราะห์ตลาดด้วย AI, ประเมินความเป็นไปได้ของเป้าหมาย, บริหารความเสี่ยง, แจ้งเตือน LINE, เทรด paper-trading อัตโนมัติ/กึ่งอัตโนมัติได้

| ชั้น | เทคโนโลยี |
|---|---|
| Frontend | Next.js 14 App Router — static export (`out/`) → Render Static Site (`tdappstatic.onrender.com`) |
| Backend | FastAPI (Python 3.11+) |
| Database | Supabase PostgreSQL — source of truth เดียวของข้อมูลบัญชี |
| Auth | PIN 6 หลัก (hash ฝั่ง server), session ใน memory + Bearer token |
| AI | DeepSeek / GLM — model + base URL ตั้งจากหน้า Settings ได้ (fallback `backend/ai.config.json`) |
| Trading | Paper-trading engine; broker adapter (MT5/OANDA/IB) หลัง interface |
| แจ้งเตือน | LINE Messaging API (6 หมวด เปิด/ปิดแยกกันได้) |
| Deploy | Render — `tdapp-api` (web + workers ฝังในตัวผ่าน `ENABLE_WORKERS=1`) + `tdapp-web` (static) |

**โหมดเทรด 3 แบบ** (`order_mode`): `auto` (AI เปิด/ปิดเอง), `semi_auto` (AI เสนอ → คนกดอนุมัติ), `manual` (AI วิเคราะห์อย่างเดียว ไม่ยิง order)

## 2. เมนูและหน้า UI (5 เมนูหลัก)

| เมนู | Route | เนื้อหา |
|---|---|---|
| 🏠 หน้าหลัก | `/` | กราฟ TradingView, Market Regime Analysis, ตัวเลือกสัญลักษณ์ (XAUUSD/EURUSD/USDJPY/GBPUSD/AUDUSD), Opportunity Score, การ์ดความพร้อม auto-trade, GoalForm, PortfolioAllocation, AiAdvicePanel |
| ⚡ สัญญาณ | `/signals` (แท็บ `สัญญาณ (สด) \| บันทึกสัญญาณ`) | การ์ด signal + ปุ่มอนุมัติ/ปฏิเสธ, เหตุผลบล็อก (`order_blocked`), preview SL/TP 3 tier (สั้น/กลาง/ยาว), SignalLevels/SltpLevels, SignalLogsPanel (tab `?tab=logs`) |
| 📊 มอนิเตอร์ | `/monitor` (แท็บ `มอนิเตอร์ \| Performance`, `?tab=performance`) | สถิติเทรด, ตารางไม้เปิดค้าง (Paper), ประวัติยิง order ล่าสุด, Risk Engine Status (ย้ายมาล่างสุด), PerformancePanel (Equity/Journal/Backtest/Kill Switch) |
| 📜 Logs | `/logs` (แท็บ quotes/news/scheduler/guard/gate/audit) | สุขภาพ quote feed, ประวัติ signal/news/scheduler/guard, risk logs |
| ⚙️ ตั้งค่า | `/settings` | Portfolio, ระบบเทรด 38 ช่อง, preset ความเสี่ยง, ตกแต่ง (hero/bg), LINE (6 หมวด), AI model/URL, PIN, ทดสอบ DB |
| 💬 แชท | `/chat` + วิดเจ็ตลอยทุกหน้า | แชท AI แบบ stream, จำ 20 ข้อความใน localStorage |

Route เก่า `/market` `/risk` `/signal-logs` `/performance` = redirect stub (client redirect ชี้ `*.html` รองรับ static hosting + bookmark เก่า)

## 3. Pipeline การเทรด (Scanner → Gate → Execution → Guard)

1. **Market Scanner** (ทุก 5 นาที) วิเคราะห์สินทรัพย์ใน `allowed_assets` → trend/volatility/opportunity score (0–100) + regime + confidence → เขียน `signals` (pending, TTL 30 นาที)
2. **Auto Trader** (ทุก 1 นาที) หยิบ pending ที่ยังไม่หมดอายุ → ผ่าน gate ทั้งหมด → `execution.execute_signal` เปิด order เดียวกันกับปุ่ม Approve (single path)
3. **Gate บล็อก order** (`execution._gate_blocked`): pause → kill switch → frequency (daily/weekly) → **cooldown เข้าใหม่ (Gate 2b)** → news → correlation → risk → heat — เหตุผล 2 ข้อแรกถูก log เป็น `order_blocked` และโชว์บนการ์ด signal ช่อง `⏸ order_blocked`
4. **Position Guard** (ทุก 1 นาที) ดูแลไม้เปิด: breakeven / trailing ladder / partial / time-stop / Smart Exit / left-behind → ขยับ SL (event `sl_moved`) หรือปิดไม้ (event `closed`)
5. **Portfolio Monitor** (ทุก 1 นาที) คำนวณ drawdown/open-risk → ละเมิดลิมิต = auto-pause + ปิด + แจ้ง LINE

### Gate กันเปิดซ้ำ (Re-entry cooldown — Gate 2b, `4084011`)
- ตั้งค่า `reentry_cooldown_min` (default 30, preset conservative 60 / moderate 30 / aggressive 15, 0 = ปิด)
- วัดจาก `paper_trades.closed_at` ล่าสุดของ asset เดียวกัน — กันวงจร "guard ปิด → trader ยิง signal เดิมซ้ำใน 1 นาที"
- แสดงใน `signal.order_blocked` ทั้งบนการ์ดและ API ("เพิ่งปิด {asset} ไป X นาที — รอ cooldown …")

## 4. Background workers (รันใน `tdapp-api` ตัวเดียว)

| Worker | ทุก | หน้าที่ |
|---|---|---|
| market_scanner | 5 นาที | วิเคราะห์ → score + signal pending |
| news_analysis | 15 นาที | CPI/GDP/NFP/FOMC/geopolitics → sentiment |
| portfolio_monitor | 1 นาที | drawdown/open-risk → auto-pause/close/notify |
| notifications | 1 นาที | ส่งคิว LINE (critical = ทันที) |
| auto_trader | 1 นาที | เปิด order อัตโนมัติผ่าน gate |
| position_guard | 1 นาที | breakeven/trailing/partial/time-stop/smart-exit |
| calendar_sync | 6 ชม. | sync ปฏิทินข่าว → news block/caution |
| daily_digest | 60 นาที | สรุปประจำวัน |
| log_maintenance | 10 นาที | purge log เกิน TTL (signal_logs 7 วัน ฯลฯ) |

ห้ามรัน worker ซ้อน 2 ที่ (comment ใน `render.yaml`)

## 5. Engines

- **StrategyEngine** — สร้าง signal + confidence + Entry/SL/TP (ATR-based 3 tier สั้น ×1.0 / กลาง ×1.5 / ยาว ×2.0), Strategy D ทอง: `gold_breakout_only` (XAUUSD ออก signal เฉพาะ breakout/retest ของ high 20 แท่ง, SL อิงแนวทะลุ + buffer 0.5×ATR)
- **RiskEngine** — risk/trade %, daily/weekly/monthly loss, max drawdown, drawdown throttle, correlation cap, open-risk — เกินลิมิต = pause + `TRADING PAUSED — MANUAL REVIEW REQUIRED` + LINE
- **SmartExit** (`smart_exit.evaluate_exit`, ทุก guard cycle) — คะแนนถือต่อ 0–100: ต่ำกว่า `exit_score_close` (default 45) = ปิด; profit-protect (≥2R คุณภาพไม่ใช่ High = แบ่งปิด 50%); trend-reversal (opp < 50 = 1 vote, 2/4 votes = ปิด); news-exit (ปฏิทิน DANGER + กำไร ≥1R = ปิด); volatility-exit (ATR% สูง + กำไร = แบ่งปิด); time-stop (อายุ > `max_hold_days` แต่กำไร ≥ `time_stop_min_r` = ยกเว้น); R-ladder trailing (1R→BE / 2R→+1R / 3R→+2R, ปิดได้ด้วย `trailing_ladder=False`)
- **Left-behind primary** ("no position left behind", migration 032) — กำไร < `no_behind_min_r` (0.5R) และอายุ > `no_behind_hold_mult` (1.75×) × ค่าเฉลี่ยถือครอง (floor `no_behind_min_days` 2 วัน) = ปิดเอาทุนคืน (ยิงก่อน time-stop ~1–2 วัน)
- **GoalEngine / PortfolioEngine** — ประเมินโอกาสถึงเป้า (best/normal/worst case + คำเตือนความเสี่ยง), แนะนำสัดส่วนพอร์ต

## 6. Position management (guard)

- **Breakeven** (`breakeven_trigger_r` default 1.0R) — ถึงเป้า ย้าย SL มาทุน (0 = ปิด)
- **Trailing** (`trail_atr_mult` 2.0×ATR) หรือ ladder 1R/2R/3R
- **Partial close** (`partial_trigger_r` + `partial_close_pct`, default ปิด) — ปิดบางส่วนที่ TP1 แล้ว trail ที่เหลือ
- **Time stop** (`max_hold_days` 5 วัน, 0 = ปิด) — ตัดไม้ค้างนานไม่ไปไหน
- **Ticket ไม่รีไซเคิล** — boot seed `_seq` ให้พ้น ticket สูงสุดใน `paper_trades` + `signal_logs`; `close_trade_rows` ปฏิเสธปิดถ้า asset/ฝั่งไม่ตรง; monitor รับ log เข้า timeline เฉพาะ asset ตรง + ไม่เก่ากว่าเวลาเปิดแถว (กันประวัติไม้เก่าโผล่ใต้ไม้ใหม่หลัง restart)

## 7. Paper-trading สมจริง + ต้นทุน

- สเปรดจำลองต่อ fill (`paper_spread` global + `spread_overrides` ราย symbol + `DEFAULT_SPREADS`; BUY +spread/2, SELL −spread/2; ขาออก `paper_exit_spread_mult` 0.5 = ครบ round-trip)
- คอมมิชชัน `paper_commission_per_lot` ($3.5/lot/side)
- Sizing จาก `risk_per_trade_pct` (default 1.0%) + `min_lot` (0.01) + `min_lot_gold`, clamp ระยะ SL (`sl_distance_min/max_pct`), **SL risk cap** (`sl_cap_enabled` — SL กว้าง + min_lot ดันความเสี่ยงเกินงบ → หด SL ให้พอดีงบก่อน sizing, TP คำนวณใหม่ที่ RR เดิม)
- RR target (`rr_target` 2.0, clamp ≥ 0.5) — TP = ระยะ SL × RR

## 8. หน้า Monitor (ฟีเจอร์ละเอียด)

- ตารางไม้เปิด: SL/TP, ราคาปัจจุบัน, uPnL, R-multiple, Smart Exit badge (score/ถือต่อ), RPriceBadge + CalcNotes (7 ขั้นตอนวิธีคำนวณ), CopyNum, Ticket, ปุ่มปิดรายตัว (popup ยืนยัน), ปิดกลุ่ม (ทั้งหมด/กำไร/ขาดทุน + popup สรุป), รีเซ็ตสถิติ (ลบ closed + reseed equity), กดแถวดูกราฟแท่งเทียน (PositionChartModal + spread/buy-sell)
- **ไทม์ไลน์ SL/TP** (MoveTimelineBadge 🔔): badge นาฬิกา + popup (SL เริ่ม→ปัจจุบัน, เวลา/เหตุผล/ทำไมขยับ + ไทม์ไลน์จาก signal-logs จัดกลุ่มตาม ticket) — portal to body (กัน `.panel` backdrop-filter ขัง fixed)
- **ตารางประวัติยิง order**: แถวปิดโชว์ **เวลาปิด** (`closed_at`, hover ดูเวลาเปิด) + คอลัมน์ **Ticket** (`75a222e` — เดิมโชว์เวลาเปิดทุกแถว ทำให้ badge (3) ของ PAPER-000002 ดูเหมือนติดผิดแถว PAPER-000050) + CloseReasonBadge popup อธิบายเหตุผลปิดด้วย exit-rule จริง
- Risk Engine Status ล่างสุด (risk level/drawdown/daily-weekly-monthly/open-risk $ + งบ daily เหลือ)
- Performance tab: Equity Curve (SVG), Journal, Backtest Center (backtest + walk-forward), Kill Switch

## 9. หน้า Signals / Logs / Settings / Chat / LINE

- **Signals**: การ์ดแสดง confidence, entry/SL/TP 3 tier, RR, เหตุผล (`reason` + `score_reasons`), `order_blocked` (⏸), เวลาหมดอายุ, live price + feed status; Approve → เส้นทางเดียวกับ auto-trader
- **Logs**: signal-logs (created/order_opened/order_blocked/sl_moved/closed/expired), quote-logs (feed health), news-logs, scheduler-logs (กัน log stall), risk-logs, guard/gate/audit
- **Settings (38 ช่อง + preset)**: Portfolio (capital/target/drawdown/mode/allowed assets/backtest), ระบบเทรด (confidence ทองแยก, opportunity, จำนวนไม้ daily/weekly/open, risk/trade, **cooldown**, min lot ทองแยก, breakeven/trailing/partial/hold/RR, smart-exit ทั้งชุด, gold breakout, spread/commission, kill limits + expand TTL/auto-apply, news block/caution, correlation, SL mode/clamp/cap, refresh intervals, AI model/URL), preset Conservative/Moderate/Aggressive, ตกแต่ง (hero zoom/height/dim + bg + event `tdapp:*-changed`), LINE 6 หมวด (trade opened/closed, stop-loss, risk, digest, summary — ปิดหมวดไหนไม่คิวไม่ push), PIN manager, ทดสอบ DB
- **AI Chat**: หน้า + วิดเจ็ตลอย, stream `POST /api/chat/stream`, typing indicator + ตัวนับวินาที, history 20 ข้อความ; ลำดับ model/URL: Settings (DB) → `ai.config.json` → provider default; ปุ่มทดสอบ (ยิงค่าที่ยังไม่ save แล้วคืนค่าเดิม); key อยู่ env `AI_API_KEY` เท่านั้น
- **LINE**: push เปิด/ปิดไม้, SL, risk alert, ขยายลิมิต (Approve/Reject หมดอายุตาม `kill_expand_ttl_min` 180 นาที + auto-apply ครั้งเดียว/24 ชม.), daily digest/summary, drawdown warning; คำสั่ง `/portfolio /market /positions /risk /summary /pause /resume`; webhook @mention ในกลุ่ม (`LINE_BOT_USER_ID`), targets/events/simulate/diag/test endpoints

## 10. ราคา (Quotes) + ความทนทาน feed

- หลาย source fallback (Twelve Data สำหรับทอง + exchangerate/Yahoo/Frankfurter สำหรับ FX) → `QuoteFeedStatus` (ok/error + failed_assets + message) + banner บน UI; mark fallback chain: spot → daily-close → broker → entry (badge บอกที่มาราคา)
- `supported assets` + `allowed_assets` (DB, migration 021) คุมจักรวาลเทรด; SL/TP re-anchor ด้วย `fetch_trusted_spot` ก่อนเปิดจริง

## 11. Auth + ข้อมูล

- PIN 6 หลัก hash ฝั่ง server (`008_pin_auth.sql`), token ใน `localStorage`, session ใน memory (deploy backend ใหม่ = login ใหม่, 401 พร้อมกันหลัง deploy = ปกติ)
- Supabase = source of truth (capital/PnL/equity/paper_trades/signals/settings ส่วนใหญ่); localStorage มีแค่ wallpaper/hero/dim/session/chat-history (migration 019 ย้าย refresh interval ลง DB, 034 ย้าย AI model/URL ลง DB)

## 12. Database (migration 001–040)

001 schema ตั้งต้น · 002 worker tables · 004 RLS insert · 005 extended trading · 006/007 settings+autotrader · 008 PIN · 009/010 signal expired/approved · 011 quote logs · 012 min-conf ทอง · 013 signal_logs · 014 SL mode · 015/016 min lot (+ทอง) · 017 position mgmt · 018 line targets · 019 UI prefs ลง DB · 020 หมวดแจ้งเตือน · 021 allowed assets + SL/TP move tracking · 022 line notify fix · 023 max hold · 024 gold breakout · 025 SL clamp · 026 score reasons · 027 spread overrides · 028 RR target · 029 smart exit · 030 scheduler logs · 031 source ขยาย · 032 left-behind primary · 033 paper costs · 034 AI chat settings · 035 SL risk cap · 036/037/039 kill-expand confirm/TTL/auto-apply · 038 risk event user text · **040 reentry cooldown** (`reentry_cooldown_min` default 30)

## 13. API (FastAPI routers)

- `auth`: status/login/set-pin/logout · `settings`: get/put/reset/presets/preset/{profile} · `signals`: latest/approve · `trading`: monitor/frequency/order-plan/extended-open/correlation/calendar/session/kill-switch/risk-officer/pause/limit-expand(+decide)/positions (close/levels/close-all/close-group)/stats/reset/equity-curve/signal-report/journal(+post)/backtest/walk-forward/paper-trading/extended-analysis · `risk`: check · `market`: summary/candles · `goal`: assess · `portfolio`: recommend · `chat`: +stream · `ai`: explain/test · `webhook`: LINE webhook/targets(+post/delete)/events/simulate/diag/test · `system`: db-check/counts/scan-now/rehydrate-book/guard-now/autotrader-dry-run/quote-logs/signal-logs/news-logs/quote-test/scheduler-logs/risk-logs

## 14. คุณภาพ + Deploy

- Backend pytest (~800+ tests ใน `backend/tests/` — cooldown 7 เคส, ticket-recycling, ticket seed, realized stats, smart-exit, RR, costs ฯลฯ) · frontend `npx tsc --noEmit` + `next build` (13/13 routes)
- `backend/scripts/` มี probe ตรวจ prod (check_*, e2e_probe_post_deploy, probe_line_*)
- Deploy: push `master` → Render build api (pip + uvicorn, health `/health` มี commit/workers/ai_config) + static web (`out/` ~5–10 นาที); ตรวจ deploy ด้วย ASCII marker ใน JS chunk (ไทยโดน \\u-escape, hash เปลี่ยนทุก build)
- UI: iOS Liquid Glass Dark (`.panel` แก้วฝ้า blur 10px, aurora, accent `#0a84ff`/profit `#30d158`/loss `#ff453a`, SVG monotone 36 ไอคอน, มือถือ-first ≥44px, `scroll-x-thin`, ห้าม emoji ในปุ่ม)
