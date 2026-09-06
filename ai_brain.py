import json
import requests

from config import DEEPSEEK_API_KEY


DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"


def get_ai_decision(market_data, symbol):
    """
    Send multi-timeframe market data to DeepSeek and receive
    a structured trading decision.

    Args:
        market_data (dict): Output from fetch_multi_timeframe_data()
        symbol (str): Trading symbol, e.g. EURUSDm

    Returns:
        dict: {
            "signal": "BUY",
            "confidence_score": 85,
            "logic": "..."
        }
    """

    daily_csv = market_data["daily_csv"]
    hourly_csv = market_data["hourly_csv"]

    system_prompt = f"""
You are an elite quantitative trader and market analyst.

You are analyzing the specific trading instrument: {symbol}.

Your task is to analyze the provided market data using:
- Price action
- Market structure
- Trend direction
- Momentum
- Support and resistance
- Higher highs / higher lows
- Lower highs / lower lows
- Potential breakouts or reversals

The Daily timeframe represents the macro trend.
The H1 timeframe represents the micro momentum.

Based ONLY on the supplied market data, determine the highest-probability
trading signal for {symbol}.

You MUST return your decision in EXACTLY this JSON format:

{{
    "signal": "BUY",
    "confidence_score": 85,
    "logic": "2 sentences explaining your reasoning"
}}

Rules:
1. "signal" MUST be exactly one of: "BUY", "SELL", or "HOLD".
2. "confidence_score" MUST be an integer from 0 to 100.
3. "logic" MUST contain exactly 2 sentences.
4. Do NOT include markdown.
5. Do NOT include code fences.
6. Do NOT include any text before or after the JSON.
7. Return valid JSON only.
"""

    user_prompt = f"""
Analyze {symbol} using the following market data.

=== DAILY TIMEFRAME — LAST 10 CANDLES ===
{daily_csv}

=== H1 TIMEFRAME — LAST 24 CANDLES ===
{hourly_csv}

Return ONLY the required JSON decision.
"""

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        "temperature": 0.1,
        "response_format": {
            "type": "json_object"
        },
    }

    try:
        response = requests.post(
            DEEPSEEK_API_URL,
            headers=headers,
            json=payload,
            timeout=30,
        )

        response.raise_for_status()

        result = response.json()

        ai_content = result["choices"][0]["message"]["content"]

        # Parse the AI response as JSON
        decision = json.loads(ai_content)

        # Validate the required fields
        required_fields = [
            "signal",
            "confidence_score",
            "logic",
        ]

        for field in required_fields:
            if field not in decision:
                raise ValueError(
                    f"AI response missing required field: {field}"
                )

        # Validate signal
        if decision["signal"] not in {"BUY", "SELL", "HOLD"}:
            raise ValueError(
                f"Invalid signal returned: {decision['signal']}"
            )

        # Validate confidence score
        confidence = decision["confidence_score"]

        if not isinstance(confidence, int):
            raise ValueError(
                "confidence_score must be an integer"
            )

        if not 0 <= confidence <= 100:
            raise ValueError(
                "confidence_score must be between 0 and 100"
            )

        return decision

    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"DeepSeek API request failed: {e}"
        ) from e

    except (KeyError, json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(
            f"Invalid AI response: {e}"
        ) from e