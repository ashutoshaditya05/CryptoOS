from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, localcontext
from functools import cache
from random import Random

from app.backtest.engine import SignalAt, simulate
from app.backtest.metrics import percentile
from app.backtest.models import (
    BacktestRequest,
    BootstrapResult,
    Metrics,
    ResearchResult,
    ResearchSettings,
    SimulationResult,
    SimulationSettings,
    WindowResult,
)
from app.market.indicators import INDICATOR_CONTEXT
from app.strategies.ema_pullback import EmaPullback
from app.strategies.engine import Replay
from app.strategies.models import PullbackSettings


def block_bootstrap(
    trade_returns: Sequence[Decimal], settings: ResearchSettings
) -> BootstrapResult | None:
    samples = len(trade_returns)
    if samples < max(5, settings.block_size):
        return None

    rng = Random(settings.seed)
    returns, drawdowns = [], []
    with localcontext(INDICATOR_CONTEXT):
        for _ in range(settings.bootstrap_paths):
            equity = peak = Decimal(1)
            worst_drawdown = Decimal(0)
            for index in range(0, samples, settings.block_size):
                start = rng.randrange(samples)
                for offset in range(min(settings.block_size, samples - index)):
                    equity *= 1 + trade_returns[(start + offset) % samples]
                    peak = max(peak, equity)
                    worst_drawdown = max(worst_drawdown, (peak - equity) / peak)
            returns.append(equity - 1)
            drawdowns.append(worst_drawdown)

        return BootstrapResult(
            paths=settings.bootstrap_paths,
            block_size=settings.block_size,
            seed=settings.seed,
            trade_samples=samples,
            return_p05=percentile(returns, Decimal("0.05")),
            return_p50=percentile(returns, Decimal("0.50")),
            return_p95=percentile(returns, Decimal("0.95")),
            drawdown_p95=percentile(drawdowns, Decimal("0.95")),
        )


def validate_research(replay: Replay, settings: ResearchSettings) -> None:
    if len(replay.candles) < 20:
        raise ValueError("Research needs at least 20 stored replay candles.")
    if settings.pullback_candidates and not isinstance(replay.strategy, EmaPullback):
        raise ValueError("Candidate parameter settings apply only to the EMA pullback strategy.")


def research_report(
    replay: Replay,
    request: BacktestRequest,
    settings: ResearchSettings,
    primary: SimulationResult,
    signal_at: SignalAt,
) -> ResearchResult:
    step = replay.timeframe.duration
    slots = (replay.end - replay.start) // step
    train_end, holdout_start = 3 * slots // 5, 4 * slots // 5
    candidates: list[tuple[PullbackSettings | None, SignalAt]] = [
        (request.pullback_settings, signal_at)
    ]
    for candidate in settings.pullback_candidates:
        candidates.append((candidate, cache(replay.with_settings(candidate).signal_at)))

    def at(slot: int) -> datetime:
        return replay.start + step * slot

    def metrics(start: int, end: int, decisions: SignalAt) -> Metrics:
        first, last = at(start), at(end)
        bars = [candle for candle in replay.candles if first <= candle.open_time < last]
        return simulate(
            replay, request.simulation, signal_at=decisions, start=first, end=last, candles=bars
        ).metrics

    def select(end: int) -> tuple[PullbackSettings | None, SignalAt, Metrics | None]:
        if len(candidates) == 1:
            return (*candidates[0], None)
        scores = [
            (params, decisions, metrics(0, end, decisions)) for params, decisions in candidates
        ]
        return max(scores, key=lambda row: row[2].return_fraction)

    params, decisions, train_metrics = select(train_end)
    chronological = [
        WindowResult(
            name="train",
            start=at(0),
            end=at(train_end),
            selected_settings=params,
            metrics=train_metrics or metrics(0, train_end, decisions),
        ),
        WindowResult(
            name="validation",
            start=at(train_end),
            end=at(holdout_start),
            selection_start=at(0),
            selection_end=at(train_end),
            selected_settings=params,
            metrics=metrics(train_end, holdout_start, decisions),
        ),
        WindowResult(
            name="holdout",
            start=at(holdout_start),
            end=at(slots),
            selection_start=at(0),
            selection_end=at(train_end),
            selected_settings=params,
            metrics=metrics(holdout_start, slots, decisions),
        ),
    ]

    walk_forward = []
    first_test = train_end // 2
    span = holdout_start - first_test
    for index in range(settings.walk_forward_folds):
        first = first_test + span * index // settings.walk_forward_folds
        last = first_test + span * (index + 1) // settings.walk_forward_folds
        params, decisions, _ = select(first)
        walk_forward.append(
            WindowResult(
                name=f"fold_{index + 1}",
                start=at(first),
                end=at(last),
                selection_start=at(0),
                selection_end=at(first),
                selected_settings=params,
                metrics=metrics(first, last, decisions),
            )
        )

    base_config = request.simulation.model_dump()
    costs = ("buy_fee_bps", "sell_fee_bps", "spread_bps", "slippage_bps")
    low = SimulationSettings.model_validate(base_config | dict.fromkeys(costs, Decimal(0)))
    high = SimulationSettings.model_validate(
        base_config | {name: min(Decimal(2_000), base_config[name] * 2) for name in costs}
    )
    return ResearchResult(
        chronological=chronological,
        walk_forward=walk_forward,
        cost_sensitivity={
            "zero_bps": simulate(replay, low, signal_at=signal_at).metrics,
            "base": primary.metrics,
            "higher_costs": simulate(replay, high, signal_at=signal_at).metrics,
        },
        block_bootstrap=block_bootstrap(
            [trade.return_on_equity for trade in primary.trades], settings
        ),
    )
