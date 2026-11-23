from app.strategies.ema_pullback import EmaPullback

DEFAULT_STRATEGY_VERSION = EmaPullback.version
STRATEGIES = {DEFAULT_STRATEGY_VERSION: EmaPullback()}


def get_strategy(version: str) -> EmaPullback:
    try:
        return STRATEGIES[version]
    except KeyError as exc:
        raise ValueError(f"Unknown strategy version {version!r}.") from exc
