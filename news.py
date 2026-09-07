"""
News extension point (Phase 11).

Nothing here fetches news today. The purpose of this module is to give
the rest of the system ONE place to ask "what is the news context for
this symbol right now?", so adding a real provider later is a single
class plus one line in PROVIDERS - not a change scattered across
ai_brain, main and the database.

The contract:

    provider.get_context(symbol, features) -> dict | None

A returned dict is stored verbatim in decisions.news_json and, if it
contains a "summary" string, injected into the DeepSeek prompt. The
"condition" key (e.g. "high_impact_window") is denormalised onto trades
and experiences so analytics can slice by it later.
"""

import config


class NewsProvider:
    """Interface every provider implements."""

    name = "base"

    def get_context(self, symbol, features=None):
        raise NotImplementedError


class NullNewsProvider(NewsProvider):
    """
    Default. Returns None, so the prompt and the database look exactly
    as they did before news existed.
    """

    name = "null"

    def get_context(self, symbol, features=None):
        return None


# ---------------------------------------------------------------------
# Registry
#
# To add a real provider:
#   1. Subclass NewsProvider and implement get_context().
#   2. Register it here under a short key.
#   3. Set NEWS_ENABLED=true and NEWS_PROVIDER=<key> in .env.
#
# No other file needs to change.
# ---------------------------------------------------------------------

PROVIDERS = {
    "null": NullNewsProvider,
}

_provider = None


def get_provider():
    """Resolve the configured provider, once."""

    global _provider

    if _provider is not None:
        return _provider

    if not config.NEWS_ENABLED:
        _provider = NullNewsProvider()
        return _provider

    provider_class = PROVIDERS.get(config.NEWS_PROVIDER, NullNewsProvider)

    _provider = provider_class()

    return _provider


def get_news_context(symbol, features=None):
    """
    Safe entry point used by the trading loop.

    A failing news provider must never take down a trading cycle, so
    every error is swallowed into None.
    """

    try:
        return get_provider().get_context(symbol, features)
    except Exception as error:                      # noqa: BLE001
        print(f"[NEWS] provider failed for {symbol}: {error}")
        return None


def news_condition(news_context):
    """Short label denormalised onto trades/experiences for analytics."""

    if not news_context:
        return None

    return news_context.get("condition") or "unclassified"
