# TradeOS — Codex Project Handoff

**Status:** Archived spot-research plan. See the [README](README.md) for the active BTCUSD order-book workflow.

**Updated:** 2026-11-23  
**Owner/use:** Single person, private use only. **Not** a SaaS, public product, or multi-user platform.

## 1. Mission

Build a private, self-hosted, AI-assisted quantitative trading and research system covering **BTC, ETH, and Gold**. The system must acquire historical and live market data, evaluate statistically testable intraday strategies, run realistic historical backtests and prospective paper trading, generate BUY/SELL/HOLD signals, support optional human approval, and eventually support tightly controlled automatic execution on eligible instruments.

**Important:** Codex is an engineering assistant, not a profitable trading oracle. No claim of guaranteed profit or fixed monthly return. A functional implementation is not evidence of positive expectancy. Research, risk, and actual execution must be auditable and reproducible.

## 2. Locked requirements and design choices

| Area | Decision |
|---|---|
| Audience | One owner; private, personal use |
| Market focus | BTC/USDT, ETH/USDT spot; XAU/USD for gold research/reference and paper simulation initially |
| Instrument policy | Long/flat spot only (SELL closes a long; no shorts, no leverage). Gold live execution disabled until a legal/eligible tradable instrument and broker are explicitly chosen. |
| Trading cadence | Intraday; signals on closed 5m and 15m candles with 1h/4h context |
| Operation modes | Signal-only; paper-trading with optional manual approval; automatic paper trading; live trading disabled by default |
| Technical base | Python 3.12+, FastAPI, Pydantic v2, CCXT where supported, SQLAlchemy, Alembic, Pytest, Ruff, Docker Compose |
| UI | Simple private Streamlit dashboard initially (Next.js optional only if needed later) |
| Storage | SQLite for local metadata, orders and ledger with WAL/single writer; Parquet for large historical datasets. PostgreSQL optional later if concurrency/scale requires it. |
| Deployment | Local first, then private Ubuntu/AWS EC2; TLS/private network; no public-facing trading controls |
| AI | Claude/OpenAI API optional for hypothesis generation, experiment design and explanation; deterministic Python for signals, fills, money and risk |
| Risk | Risk engine has veto power. AI cannot change limits, authorize deployment, access exchange secrets, or place live orders directly. |

### Scope adjustment from prior conversation
A much larger enterprise multi-agent/Next.js/PostgreSQL architecture was considered first. **Do not build enterprise/SaaS features** for this project: no multi-tenant accounts, RBAC/team management, public strategy marketplace, API gateway, distributed microservices, or heavy event infrastructure. Keep a well-structured modular monolith with a background scheduler/worker. Preserve the hard parts that directly affect trading safety and correctness.

## 3. System architecture

```text
Crypto Exchange REST/WebSocket      Gold Market Data Provider
               \                         /
                -> Ingestion / normalization
                   - UTC timestamps, precision, instrument metadata
                   - completeness checks and gap recovery
                   - only closed candles trigger decisions
                -> Parquet historical data + SQLite durable metadata
                -> Feature/indicator computation
                -> Market regime detection
                -> Versioned deterministic strategy engine
                -> Structured signal candidates
                -> Portfolio and risk engine (hard veto)
                -> Execution selector
                   |- Signal-only -> Alerts/dashboard
                   |- Paper broker -> simulated orders/fills/ledger
                   |- Live adapter -> DISABLED until separate explicit approval
                -> Reconciliation / accounting / audit / metrics
                -> Private Streamlit dashboard, Telegram notifications

Isolated research loop:
Historical data -> AI hypothesis proposal -> parameterized Python strategy
-> backtest -> walk-forward/holdout -> cost sensitivity -> paper validation
-> human review -> optional strategy promotion
```

Use the **same strategy and risk logic** in historical replay, paper mode, and eventually live mode. Backtest and paper/live broker adapters can differ, but business rules must not silently diverge.

## 4. Logical modules / agents

1. **Market intelligence:** Trend, volatility, regime, volume, market status. Primarily deterministic.
2. **Technical features:** EMA, RSI, ATR, ADX, VWAP, rolling statistics, basic market structure. Deterministic and unit-tested.
3. **Strategy research AI:** Proposes falsifiable trading hypotheses, parameter ranges, ablation studies, and explanations. Can write experimental code in an isolated development workspace; cannot promote it to live.
4. **Signal evaluation:** Applies approved versioned strategies to closed candles. Deterministic.
5. **Risk and portfolio:** Validates every order, position size, cash, correlation/concentration, stop, daily loss cap, and halt state. Deterministic, non-bypassable.
6. **Execution worker:** Paper broker for MVP; later eligible spot adapter. Must use idempotent IDs and reconcile unknown orders before retrying.
7. **Performance review AI:** Summarizes backtest and paper-trading results and failures, but does not make claims of guaranteed performance.

Do not create a separate LLM for each logical function: simpler is better.

## 5. Strategies to research first (not assumed profitable)

### A. EMA pullback (implement first)
- 1h EMA50 > EMA200 confirms broad uptrend.
- 15m EMA20 > EMA50.
- Completed signal candle pulls back/reclaims EMA20.
- Configurable ADX and volatility filters.
- Stop derived from ATR or explicit invalidation; target/trailing or trend-based exit.

### B. Range breakout/retest
- Define range by historical window using only past data.
- Completed candle breaks above range high.
- Volume confirmation only when provider volume is meaningful.
- Optional bounded-time retest; configurable spread/slippage gate.
- Long-only, protective stop and time exit.

### C. VWAP mean reversion
- Only when regime indicates range rather than strong trend.
- Price deviates sufficiently below anchored VWAP, RSI/volatility filter and reversal confirmation.
- Anchor crypto VWAP at specified UTC session, not unspecified exchange day.
- Gold session/volume assumptions must be explicit; do not assume centralized spot gold volume.

Market regime selector: uptrend -> pullback; range expansion -> breakout; sideways -> mean reversion; excessive volatility, unreliable data, or insufficient history -> no trade.

For MVP only one approved strategy per symbol/timeframe should be eligible to place orders at once. Prevent duplicate/conflicting strategy orders.

## 6. Quantitative research and mathematical work

Implement and test:
- Descriptive and inferential statistics: returns, distributions, volatility, rolling correlation, confidence intervals, expected value and break-even probability.
- Risk statistics: max drawdown, Sharpe, Sortino, net expectancy, profit factor, turnover, exposure, hit rate, value at risk with explicit caveats.
- Monte Carlo simulations / bootstrap of trade returns and drawdown risk; account for serial dependence when applicable.
- Walk-forward optimization; chronological train/validation/untouched test partitions; avoid selection bias and overfitting.
- Regression, Bayesian updates and regime models as **later research modules**, not prerequisites to a reliable MVP.
- ML (gradient boosting, HMM/GARCH, RL) only when simpler baselines are validated and genuinely outperformed. No assumption complexity improves returns.

Illustrative expectancy formula: `E = P(win)*AvgWin - P(loss)*AvgLoss - costs` (ensure consistent units and avoid double-subtracting costs if net returns already used).

Backtests must model fees, spread, slippage, next-eligible-event fills, order quantity precision, minimum notional, possible nonfills/partial fills, conservative intrabar stop/target ambiguity, holding/capital restrictions, and cash/equity evolution. A signal calculated from a candle's close **cannot be filled earlier inside that candle**. Walk-forward and out-of-sample results must be reproducible by experiment and code version/hash.

## 7. Paper broker and order state machine

`CREATED -> RISK_APPROVED -> SUBMITTED -> ACCEPTED -> PARTIALLY_FILLED/FILLED`, with `REJECTED`, `CANCELLED`, and `UNKNOWN -> RECONCILED` paths. Real-world venue states may differ; adapters translate to a stable internal model.

Paper broker must:
- Start with configurable synthetic quote cash (example 10,000 USDT); no real keys needed.
- Maintain immutable cash/fill ledger and derived long positions; track fees and precision.
- Simulate limit orders that may not fill, market orders at realistic bid/ask or conservative candle-derived prices, slippage, partial fills, stop/target events, and cancellations.
- Preserve unique client order IDs, atomic order/ledger transactions, restart recovery and reconciliation.
- Mark to market on valid timestamps, distinguish realized/unrealized P&L.
- Offer signal-only, approve-first, and automatic **paper** modes.

Paper trading is not broker testnet trading; the local paper broker should work without an exchange sandbox. Testnet may be used later for API integration separately.

## 8. Risk policy (initial paper-mode defaults, not validated optimal parameters)

```yaml
risk:
  max_risk_per_trade: 0.005
  max_daily_loss: 0.02
  max_total_exposure: 0.50
  max_asset_exposure: 0.25
  max_open_positions: 2
  consecutive_loss_pause: 3
  leverage_enabled: false
  short_selling_enabled: false
execution:
  environment: paper
  mode: approval
  live_trading_enabled: false
```

Position sizing must account for stop distance, estimated full round-trip costs, free cash, maximum allocation, instrument filters and rounding downward. Reject undefined/invalid stops, stale market data, incomplete candles, unsupported instruments, excessively wide spread or expected slippage, duplicate orders, loss-limit violations, irreconcilable broker state, or kill-switch activation.

BTC and ETH correlated concentration should be evaluated at portfolio level. Daily-loss calculations need clearly defined equity reference time, timezone (UTC), realized and unrealized loss treatment, and recovery behavior. Stops may slip or fail; never imply fixed maximum loss is guaranteed. Live mode requires separate human activation, key/venue controls and eligibility checks and is **not** in first MVP.

## 9. Data contracts

Use Pydantic and Decimal for money and quantities; UTC timezone-aware datetimes everywhere. Suggested interfaces:

```python
class Candle(BaseModel):
    venue: str
    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    is_closed: bool

class Signal(BaseModel):
    signal_id: str
    strategy_version: str
    symbol: str
    timestamp: datetime
    action: Literal['BUY','SELL','HOLD']
    entry_reference: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    reason_codes: list[str]
    valid_until: datetime
```

Every event/order should include event ID, schema version, source, event timestamp, correlation ID. Unique candle key `(venue,symbol,timeframe,open_time)`. Distinguish vendor reference quotes from executable broker bid/ask data. Missing volume must not silently become zero volume or be treated as verified real exchange turnover.

## 10. Suggested private repository structure

```text
tradeos/
├── README.md
├── .env.example
├── .gitignore
├── pyproject.toml
├── uv.lock
├── docker-compose.yml
├── config/
│   ├── markets.yaml
│   ├── strategies.yaml
│   └── risk.yaml
├── app/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── market/
│   │   ├── base.py
│   │   ├── crypto.py
│   │   ├── gold.py
│   │   ├── ingestion.py
│   │   ├── validation.py
│   │   └── indicators.py
│   ├── strategies/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── ema_pullback.py
│   │   ├── breakout.py
│   │   └── mean_reversion.py
│   ├── research/
│   │   ├── statistics.py
│   │   ├── monte_carlo.py
│   │   ├── walk_forward.py
│   │   └── experiments.py
│   ├── ai/
│   │   ├── research_agent.py
│   │   └── performance_agent.py
│   ├── trading/
│   │   ├── signals.py
│   │   ├── risk.py
│   │   ├── orders.py
│   │   ├── paper_broker.py
│   │   ├── broker_interface.py
│   │   ├── portfolio.py
│   │   └── reconciliation.py
│   ├── backtest/
│   │   ├── engine.py
│   │   ├── fills.py
│   │   └── metrics.py
│   ├── api/
│   │   └── routes.py
│   └── notifications/
│       └── telegram.py
├── dashboard/
│   └── streamlit_app.py
├── migrations/
├── scripts/
│   ├── backfill.py
│   └── export_report.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── backtest/
│   └── fixtures/
├── data/                   # gitignored
└── docs/
    ├── architecture.md
    ├── risk-policy.md
    ├── strategy-research.md
    └── runbook.md
```

If a design decision calls for a change, document the rationale in an ADR rather than silently overengineering.

## 11. Key API/CLI capabilities

Minimal private FastAPI endpoints: `GET /health`, `GET /markets`, `GET /candles`, `GET /signals`, `GET /positions`, `GET /orders`, `GET /portfolio`, `POST /backtests`, `GET /backtests/{id}`, `POST /paper/order-intents`, `POST /paper/approvals/{id}`, `POST /controls/kill-switch`. Protect all trade-affecting endpoints. CLI commands for ingest/backfill, run backtest, start/stop paper engine, export strategy report, and reconcile ledger.

Dashboard: market status and charts, signal log, order approvals, balances/positions, equity curve, drawdown, research experiments, strategy and risk config, alerts, and clear PAPER vs LIVE visual separation. UI should never display invented trading performance or fabricated real-time prices.

## 12. Implementation sequence and acceptance criteria

**Phase 1 — Foundation and ingestion (first task)**
- Create runnable Python project, SQLite migrations/schema, Parquet layout, FastAPI, provider adapter interfaces, CCXT historical BTC/ETH ingestion, gold adapter interface, resampling, validation, tests, and README.
- Acceptance: historical closed candles persist without duplicates; UTC and missing candle logic correct; `/health` and `/candles` work locally; test suite green; **zero live execution**.

**Phase 2 — Deterministic strategy engine**
- Indicators, EMA pullback, strategy registry/versioning, regime filter, serialized signals.
- Acceptance: repeatable signals from frozen datasets, no look-ahead, unit tests and golden fixtures.

**Phase 3 — Backtest and research**
- Event-driven replay, costs, fills, order/portfolio ledger, performance metrics, chronological splits, walk-forward, Monte Carlo, sensitivity testing.
- Acceptance: reproducible runs, baseline comparison, correct accounting, no same-bar future fills.

**Phase 4 — Paper trading and risk**
- Candle events, risk veto, paper broker, order states, manual approvals, signal-only/auto paper, alerts, restart/recovery and kill switch.
- Acceptance: weeks-long continuous paper operation possible; duplicate orders prevented; reconciliation and loss-limit tests pass.

**Phase 5 — Extend and observe**
- Breakout + VWAP strategy, gold reference data with clear units/sessions, Streamlit dashboard, experiment reports, AI hypothesis and performance agents.
- Acceptance: AI cannot bypass risk or introduce unreviewed strategy; realistic reporting.

**Phase 6 — Optional live rollout, only after prospective paper evidence**
- Confirm instrument legality/eligibility and broker capabilities for user’s jurisdiction, use no-withdrawal keys, implement real broker adapter, ensure security and independent kill switch, start with small exposure and human approval; later consider automatic mode.
- Not authorized merely by this specification.

## 13. Time and cost expectations

Prior planning estimate for solo development using Claude Code/Codex with 4–6 focused hours/day:
- Basic prototype: roughly 1–2 weeks.
- Useful private MVP: roughly 4–8 weeks.
- Advanced research and validation features: roughly 8–12 weeks total.
- Prospective paper evaluation for any evidence of robustness: **3–6+ months**, potentially longer; no reliable time-to-profitability.

Planning ranges are rough, not guarantees. Software may be free/open-source; recurring cost drivers include hosting, AI calls, gold data licensing and trading fees. No performance/earnings promise.

## 14. Security and deployment constraints

- Start local/offline replay, then private Docker Compose/Ubuntu or AWS EC2.
- Do not expose admin trading APIs publicly without appropriate network controls, authentication, encryption and strong credentials.
- Never commit or print exchange keys; keep keys away from LLM tools, CI and tests.
- Default to paper mode; testnet integration, if available, is not evidence of production readiness.
- Database backup, durable logs, monitoring/Telegram alerts, health checks, time sync, restart-safe order recovery.
- Live gold XAU/USD CFD/forex trading should not be enabled by assumption; choose a verified legally eligible instrument and broker first, especially considering Indian regulatory restrictions.

## 15. Instructions for Codex right now

1. Read this entire handoff as the source of project requirements.
2. **Implement only Phase 1** in the working directory. Do not attempt to build the whole bot in one pass.
3. Before editing, print a concise plan and list any assumptions you must make.
4. Prefer straightforward production-quality code over framework proliferation.
5. Write actual implementation + tests + exact commands to run them; do not merely produce pseudocode.
6. Run tests and report pass/fail honestly; if blocked by credentials/network say so and keep a functional offline fixture path.
7. Never invent trade returns, price data, test results, API access, or completed features.
8. Explicitly list changed files, any risks, and the next Phase 2 task when done.
9. Do not use live trading APIs or execute real orders.

### Copy-paste starter prompt

> Read `TRADEOS_CODEX_HANDOFF.md` fully. This is my private single-user trading research and paper-trading project, not a SaaS. Implement **Phase 1 only** following its architecture and acceptance criteria. Use Python 3.12+, FastAPI, SQLite, Parquet and CCXT. Make historical BTC/ETH candle ingestion, UTC normalization, duplicate/gap detection, validation, REST retrieval, adapter interfaces, and tests work end to end. Gold should have a provider-neutral data interface, not fake live quotes. Live trading is forbidden. Create a precise README and run the tests. Explain any blockers and do not proceed to Phase 2 automatically.
