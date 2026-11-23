from app.strategies.ema_pullback import EmaPullback
from app.strategies.vwap_ema_cross import VwapEmaCross

DEFAULT_STRATEGY_VERSION = EmaPullback.version
STRATEGIES = {DEFAULT_STRATEGY_VERSION: EmaPullback(), VwapEmaCross.version: VwapEmaCross()}


def get_strategy(version: str) -> EmaPullback | VwapEmaCross:
    try:
        return STRATEGIES[version]
    except KeyError as exc:
        raise ValueError(f"Unknown strategy version {version!r}.") from exc
