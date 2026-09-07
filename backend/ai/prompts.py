"""
Prompt construction for the DeepSeek call.

Kept separate from ai_brain.py so the request/validation logic and the
prompt text can be reviewed and changed independently.

Sections are appended only when they have content: with an empty
database and news disabled, the assembled prompt is identical to the
original two-CSV prompt.
"""

import json


# =====================================================================
# PROMPTS
# =====================================================================

def build_system_prompt(symbol):
    return f"""
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
    "daily_trend": "bullish",
    "h1_trend": "bullish",
    "momentum": "strong",
    "market_structure": "higher_highs",
    "market_regime": "trending",
    "setup": "breakout",
    "score": 82,
    "signal": "BUY",
    "daily_analysis": "1 sentence on the daily timeframe",
    "h1_analysis": "1 sentence on the H1 timeframe",
    "reasoning": "2 sentences explaining your reasoning"
}}

Rules:
1. "signal" MUST be exactly one of: "BUY", "SELL", or "HOLD".
2. "score" MUST be an integer from 0 to 100. It is a conviction score,
   NOT a probability, and it is not calibrated against outcomes.
3. "daily_trend" and "h1_trend": "bullish", "bearish" or "neutral".
4. "momentum": "strong", "moderate", "weak" or "neutral".
5. "market_structure": "higher_highs", "lower_lows", "ranging",
   "expanding" or "compressing".
6. "market_regime": "trending", "ranging", "choppy" or "volatile".
7. "setup": a short snake_case label, e.g. "breakout", "pullback",
   "reversal", "range_fade", "continuation", or "none".
8. "reasoning" MUST contain exactly 2 sentences.
9. Do NOT include markdown.
10. Do NOT include code fences.
11. Do NOT include any text before or after the JSON.
12. Return valid JSON only.
"""


def build_user_prompt(symbol, market_data, experience_context=None,
                      news_context=None):
    """
    Assemble the analysis request.

    Sections are appended only when they have content, so on a fresh
    database with news disabled the prompt is identical to the original
    two-CSV prompt.
    """

    daily_csv = market_data["daily_csv"]
    hourly_csv = market_data["hourly_csv"]

    sections = [
        f"Analyze {symbol} using the following market data.",
        "",
        "=== DAILY TIMEFRAME - LAST 10 CANDLES ===",
        daily_csv,
        "",
        "=== H1 TIMEFRAME - LAST 24 CANDLES ===",
        hourly_csv,
    ]

    # Engine-computed features: measurements, not opinions.
    features = market_data.get("features")

    if features:
        sections += [
            "",
            "=== ENGINE-COMPUTED FEATURES (measured, not opinion) ===",
            json.dumps({
                "atr_h1": features.get("atr_h1"),
                "atr_pct": features.get("atr_pct"),
                "volatility_bucket": features.get("volatility_bucket"),
                "computed_daily_trend": features.get("daily_trend"),
                "computed_h1_trend": features.get("h1_trend"),
                "computed_momentum": features.get("momentum"),
                "computed_market_structure": features.get("market_structure"),
                "computed_market_regime": features.get("market_regime"),
                "session": features.get("session"),
                "spread": features.get("spread"),
            }, indent=2, default=str),
            "",
            (
                "These are mechanical indicators. You may disagree with "
                "them; state why in your reasoning if you do."
            ),
        ]

    if experience_context:
        sections += ["", experience_context]

    if news_context:
        sections += [
            "",
            "=== NEWS CONTEXT ===",
            json.dumps(news_context, indent=2, default=str),
        ]

    sections += [
        "",
        "Return ONLY the required JSON decision.",
    ]

    return "\n".join(sections)
