"""
DeepSeek analysis layer.

Two behavioural rules govern this module:

  1. It NEVER raises into the trading loop. Every failure path returns
     a well-formed decision dict with signal "HOLD" and a status other
     than "ok", so a timeout, an HTTP error or malformed JSON can never
     become an order. (Phase 5 / Phase 13)

  2. The free-form reasoning the UI shows is preserved verbatim, while
     the structured fields are validated separately. A response that
     fails validation is still stored - with status "invalid" - so the
     failure is visible in the dashboard instead of disappearing.
"""

import json
import time

import requests

import config
import memory
import news
from prompts import build_system_prompt, build_user_prompt


VALID_SIGNALS = {"BUY", "SELL", "HOLD"}

TIMEFRAME_LABEL = "D1+H1"

# Enumerations the model is asked to choose from. Anything outside the
# set is kept as free text rather than rejected, so a slightly creative
# label never blocks a trade on its own.
STRUCTURED_FIELDS = (
    "daily_trend",
    "h1_trend",
    "momentum",
    "market_structure",
    "market_regime",
    "setup",
)


# =====================================================================
# VALIDATION
# =====================================================================

def _coerce_score(value):
    """
    Accept an int, an integral float, or a numeric string.

    Returns None if the value cannot be read as an integer 0-100.
    """

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        score = value

    elif isinstance(value, float):
        if value != int(value):
            return None
        score = int(value)

    elif isinstance(value, str):
        try:
            score = int(value.strip())
        except ValueError:
            return None

    else:
        return None

    if not 0 <= score <= 100:
        return None

    return score


def validate_response(parsed):
    """
    Check the structured payload.

    Returns (normalised_dict, error_message). error_message is None
    when the response is usable.
    """

    if not isinstance(parsed, dict):
        return None, "AI response was not a JSON object"

    # "signal" is the field name; "decision" is accepted as an alias
    # because the model occasionally uses it.
    raw_signal = parsed.get("signal") or parsed.get("decision")

    if not isinstance(raw_signal, str):
        return None, "AI response missing required field: signal"

    signal = raw_signal.strip().upper()

    if signal not in VALID_SIGNALS:
        return None, f"Invalid signal returned: {raw_signal!r}"

    # "score" is the field name; "confidence_score" is the legacy name.
    raw_score = parsed.get("score")

    if raw_score is None:
        raw_score = parsed.get("confidence_score")

    score = _coerce_score(raw_score)

    if score is None:
        return None, f"Invalid score returned: {raw_score!r}"

    # "reasoning" is the field name; "logic" is the legacy name.
    reasoning = parsed.get("reasoning") or parsed.get("logic")

    if not isinstance(reasoning, str) or not reasoning.strip():
        return None, "AI response missing required field: reasoning"

    normalised = {
        "signal": signal,
        "score": score,
        "reasoning": reasoning.strip(),
        "daily_analysis": parsed.get("daily_analysis"),
        "h1_analysis": parsed.get("h1_analysis"),
    }

    for field in STRUCTURED_FIELDS:
        value = parsed.get(field)

        normalised[field] = (
            value.strip().lower() if isinstance(value, str) else None
        )

    return normalised, None


# =====================================================================
# RESULT SHAPES
# =====================================================================

def _base_result(symbol, features=None, news_context=None,
                 experience_ids=None):
    """
    Every return path shares this shape.

    The legacy keys signal / confidence_score / logic are always
    present so nothing downstream breaks.
    """

    return {
        "symbol": symbol,
        "timeframe": TIMEFRAME_LABEL,
        "model": config.DEEPSEEK_MODEL,

        # legacy keys
        "signal": "HOLD",
        "confidence_score": 0,
        "logic": "",

        # structured keys
        "status": "ok",
        "ai_signal": "HOLD",
        "ai_score": 0,
        "reasoning": "",
        "daily_trend": None,
        "h1_trend": None,
        "momentum": None,
        "market_structure": None,
        "market_regime": None,
        "setup": None,
        "daily_analysis": None,
        "h1_analysis": None,

        # provenance
        "features": features,
        "news": news_context,
        "experience_ids": experience_ids or [],
        "raw_response": None,
        "latency_ms": None,
        "error": None,
    }


def _failed(symbol, status, message, features=None, news_context=None,
            experience_ids=None, raw_response=None, latency_ms=None):
    """A safe HOLD carrying the reason it failed."""

    result = _base_result(symbol, features, news_context, experience_ids)

    result.update({
        "status": status,
        "error": message,
        "logic": f"AI unavailable - holding. ({message})",
        "reasoning": f"AI unavailable - holding. ({message})",
        "raw_response": raw_response,
        "latency_ms": latency_ms,
    })

    return result


# =====================================================================
# MAIN ENTRY POINT
# =====================================================================

def get_ai_decision(market_data, symbol):
    """
    Send multi-timeframe market data to DeepSeek and receive a
    structured trading decision.

    Never raises. On any failure the returned dict has signal "HOLD"
    and a status of "invalid" or "error".
    """

    features = market_data.get("features")

    # ------------------------------------------------------------
    # Context assembly (memory + news)
    # ------------------------------------------------------------

    experiences = memory.retrieve_relevant(symbol, features)

    experience_ids = [e["id"] for e in experiences]

    experience_context = memory.build_prompt_context(experiences)

    news_context = news.get_news_context(symbol, features)

    if not config.DEEPSEEK_API_KEY:
        return _failed(
            symbol,
            "error",
            "DEEPSEEK_API_KEY is not configured",
            features,
            news_context,
            experience_ids,
        )

    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": build_system_prompt(symbol),
            },
            {
                "role": "user",
                "content": build_user_prompt(
                    symbol,
                    market_data,
                    experience_context,
                    news_context,
                ),
            },
        ],
        "temperature": config.DEEPSEEK_TEMPERATURE,
        "response_format": {
            "type": "json_object"
        },
    }

    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    started = time.monotonic()

    # ------------------------------------------------------------
    # Call
    # ------------------------------------------------------------

    try:
        response = requests.post(
            config.DEEPSEEK_API_URL,
            headers=headers,
            json=payload,
            timeout=config.DEEPSEEK_TIMEOUT,
        )

        response.raise_for_status()

        result = response.json()

    except requests.exceptions.Timeout:
        return _failed(
            symbol, "error", "DeepSeek request timed out",
            features, news_context, experience_ids,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    except requests.exceptions.RequestException as error:
        return _failed(
            symbol, "error", f"DeepSeek API request failed: {error}",
            features, news_context, experience_ids,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    except ValueError as error:
        return _failed(
            symbol, "error", f"DeepSeek returned non-JSON body: {error}",
            features, news_context, experience_ids,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    latency_ms = int((time.monotonic() - started) * 1000)

    # ------------------------------------------------------------
    # Extract + parse
    # ------------------------------------------------------------

    try:
        ai_content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        return _failed(
            symbol, "error", f"Unexpected DeepSeek envelope: {error}",
            features, news_context, experience_ids,
            raw_response=json.dumps(result, default=str)[:4000],
            latency_ms=latency_ms,
        )

    try:
        parsed = json.loads(ai_content)
    except (TypeError, json.JSONDecodeError) as error:
        return _failed(
            symbol, "invalid", f"AI response was not valid JSON: {error}",
            features, news_context, experience_ids,
            raw_response=ai_content,
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------
    # Validate
    # ------------------------------------------------------------

    normalised, error_message = validate_response(parsed)

    if error_message:
        return _failed(
            symbol, "invalid", error_message,
            features, news_context, experience_ids,
            raw_response=ai_content,
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------
    # Success
    # ------------------------------------------------------------

    decision = _base_result(symbol, features, news_context, experience_ids)

    decision.update({
        "status": "ok",

        # legacy keys, kept in sync
        "signal": normalised["signal"],
        "confidence_score": normalised["score"],
        "logic": normalised["reasoning"],

        "ai_signal": normalised["signal"],
        "ai_score": normalised["score"],
        "reasoning": normalised["reasoning"],

        "daily_trend": normalised["daily_trend"],
        "h1_trend": normalised["h1_trend"],
        "momentum": normalised["momentum"],
        "market_structure": normalised["market_structure"],
        "market_regime": normalised["market_regime"],
        "setup": normalised["setup"],
        "daily_analysis": normalised["daily_analysis"],
        "h1_analysis": normalised["h1_analysis"],

        "raw_response": ai_content,
        "latency_ms": latency_ms,
    })

    return decision
