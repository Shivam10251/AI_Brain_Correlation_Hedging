"""
The trading brain.

Takes one symbol's multi-timeframe picture, adds whatever the auditor
has learned about that symbol, and asks DeepSeek for a decision.

The learned-rules injection is what closes the loop: a setup that has
lost repeatedly arrives at the model already carrying a mandated
confidence penalty, so it has to clear a higher bar than it did the
first time.

Two guarantees this module enforces, whatever the model returns:

  * A score below CONFIDENCE_THRESHOLD becomes HOLD. Non-negotiable,
    applied after the penalties.
  * Stops must be arranged correctly around the entry. A BUY whose
    stop loss sits above the price is not a trade, it is a bug that
    the broker would reject or, worse, fill backwards. Bad geometry is
    replaced with ATR-derived levels.
"""

import json

import requests

import config
import auditor


def load_learned_rules(symbol):
    """
    Active rules that apply to this symbol.

    A rule scoped to ALL applies everywhere; anything else must match
    the symbol exactly.
    """

    document = auditor.load_rules_document()

    applicable = []

    for rule in document.get("rules", []):

        if rule.get("status") != "ACTIVE":
            continue

        target = str(rule.get("affected_symbol") or "ALL").strip()

        if target.upper() == "ALL" or target == symbol:
            applicable.append(rule)

    return applicable


def _format_rules_section(rules):
    """Render the learned rules as a prompt section."""

    if not rules:
        return (
            "ACTIVE RISK AUDIT RULES: none yet. No losing patterns have "
            "been identified for this instrument."
        )

    lines = [
        "ACTIVE RISK AUDIT RULES — MANDATORY",
        "",
        "The risk auditor has identified these recurring loss patterns "
        "from this strategy's own closed trades. If the current setup "
        "matches one, you MUST subtract the stated number of points "
        "from your confidence_score, and you MUST say so in your "
        "logic.",
        "",
    ]

    for index, rule in enumerate(rules, start=1):
        lines.append(
            f"{index}. [{rule.get('affected_symbol')}] "
            f"{rule.get('setup')}\n"
            f"   PENALTY: -{rule.get('confidence_reduction_points')} points\n"
            f"   Observed in {rule.get('sample_size')} losing trades. "
            f"{rule.get('evidence', '')}"
        )

    return "\n".join(lines)


SYSTEM_PROMPT_TEMPLATE = """
You are the lead trader of a strictly risk-managed quantitative hedge
fund, trading {symbol}.

You are not paid to have opinions. You are paid to decline marginal
setups and to take only those with a genuine structural edge. HOLD is
the correct answer most of the time, and costs nothing.

Read the Daily frame as the macro trend and the H1 frame as the micro
momentum. A signal that fights the Daily trend needs materially more
evidence than one that follows it.

Weigh in particular:
  * Price relative to EMA(200) on both frames — the trend filter.
  * RSI(14) — momentum, and whether the move is already exhausted.
  * ATR(14) — the volatility your stop has to survive. Your stop must
    sit outside ordinary noise, not inside it.
  * Relative volume — participation. Around 1.0 is normal. A breakout
    on materially less than 1.0 has no one behind it and usually
    fails.

{rules_section}

Stop placement:
  * For a BUY: stop_loss BELOW the entry price, take_profit ABOVE it.
  * For a SELL: stop_loss ABOVE the entry price, take_profit BELOW it.
  * Base both distances on H1 ATR, not on round numbers.
  * Aim for a reward-to-risk of roughly 2:1 or better.

Return RAW JSON ONLY. No markdown, no code fences, no text before or
after it. Exactly this shape:

{{
  "signal": "BUY" | "SELL" | "HOLD",
  "confidence_score": 0-100 integer,
  "stop_loss": float,
  "take_profit": float,
  "logic": "concise explanation naming the indicators that drove this, and any learned-rule deductions you applied"
}}

For a HOLD, set stop_loss and take_profit to 0.
""".strip()


def _build_user_prompt(market_data, symbol):
    h1 = market_data["h1_data"]
    daily = market_data["daily_data"]

    def fmt(value, digits=5):
        if value is None:
            return "n/a"
        return f"{value:.{digits}f}"

    return f"""
Analyse {symbol} and return your decision.

=== LIVE PRICE ===
Bid:    {fmt(market_data['bid'])}
Ask:    {fmt(market_data['ask'])}
Spread: {fmt(market_data['spread'])}

=== DAILY TIMEFRAME (macro trend) ===
Close:            {fmt(daily['close'])}
EMA(200):         {fmt(daily['ema_200'])}   (price is {daily['price_vs_ema']})
RSI(14):          {fmt(daily['rsi_14'], 2)}
ATR(14):          {fmt(daily['atr_14'])}
Relative volume:  {fmt(daily['relative_volume'], 2)}
Structure:        {daily['structure']}

=== H1 TIMEFRAME (micro momentum) ===
Close:            {fmt(h1['close'])}
EMA(200):         {fmt(h1['ema_200'])}   (price is {h1['price_vs_ema']})
RSI(14):          {fmt(h1['rsi_14'], 2)}
ATR(14):          {fmt(h1['atr_14'])}
Relative volume:  {fmt(h1['relative_volume'], 2)}
Structure:        {h1['structure']}

=== ACCOUNT ===
Equity:  {market_data['equity']:.2f}
Balance: {market_data['balance']:.2f}

Return ONLY the JSON decision.
""".strip()


def _validate_geometry(decision, market_data):
    """
    Make sure the stops are arranged correctly around the entry.

    A model that returns a BUY with its stop above the entry has
    produced something the broker will reject outright, or that would
    behave as an instant loss. Rather than discard an otherwise good
    signal, the levels are rebuilt from ATR.
    """

    signal = decision["signal"]

    if signal == "HOLD":
        decision["stop_loss"] = 0.0
        decision["take_profit"] = 0.0
        return decision

    price = market_data["ask"] if signal == "BUY" else market_data["bid"]

    atr = market_data["h1_data"].get("atr_14") or 0.0

    try:
        stop_loss = float(decision.get("stop_loss") or 0.0)
        take_profit = float(decision.get("take_profit") or 0.0)
    except (TypeError, ValueError):
        stop_loss = take_profit = 0.0

    if signal == "BUY":
        valid = 0 < stop_loss < price < take_profit
    else:
        valid = 0 < take_profit < price < stop_loss

    if valid:
        return decision

    if atr <= 0:
        # No ATR to fall back on. Refusing is the only safe answer -
        # inventing a stop distance out of nothing is how accounts die.
        decision["signal"] = "HOLD"
        decision["stop_loss"] = 0.0
        decision["take_profit"] = 0.0
        decision["logic"] = (
            f"Forced HOLD: model returned invalid stop geometry "
            f"(SL={stop_loss}, TP={take_profit}, price={price}) and no "
            f"ATR was available to rebuild it. "
            f"Original reasoning: {decision.get('logic', '')}"
        )
        return decision

    sl_distance = config.ATR_SL_MULTIPLIER * atr
    tp_distance = config.ATR_TP_MULTIPLIER * atr

    if signal == "BUY":
        decision["stop_loss"] = round(price - sl_distance, 8)
        decision["take_profit"] = round(price + tp_distance, 8)
    else:
        decision["stop_loss"] = round(price + sl_distance, 8)
        decision["take_profit"] = round(price - tp_distance, 8)

    decision["logic"] = (
        f"{decision.get('logic', '')} "
        f"[SYSTEM: model stop geometry was invalid; levels rebuilt from "
        f"{config.ATR_SL_MULTIPLIER}x/{config.ATR_TP_MULTIPLIER}x H1 ATR.]"
    ).strip()

    decision["geometry_corrected"] = True

    return decision


def _safe_hold(reason):
    """Every failure path produces a decision, not an exception."""

    return {
        "signal": "HOLD",
        "confidence_score": 0,
        "stop_loss": 0.0,
        "take_profit": 0.0,
        "logic": reason,
        "error": True,
    }


def get_ai_decision(market_data, symbol):
    """
    One trading decision for one symbol.

    Never raises. A broken API call, a malformed response or an
    out-of-range score all return a HOLD carrying the reason, so the
    loop keeps running and the dashboard shows what went wrong.
    """

    if not config.DEEPSEEK_API_KEY:
        return _safe_hold("DEEPSEEK_API_KEY is not configured.")

    rules = load_learned_rules(symbol)

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        symbol=symbol,
        rules_section=_format_rules_section(rules),
    )

    url = f"{config.DEEPSEEK_API_BASE.rstrip('/')}/chat/completions"

    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": _build_user_prompt(market_data, symbol),
            },
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    try:
        response = requests.post(
            url, headers=headers, json=payload,
            timeout=config.DEEPSEEK_TIMEOUT,
        )

        response.raise_for_status()

        content = response.json()["choices"][0]["message"]["content"]

        decision = json.loads(content)

    except requests.exceptions.RequestException as error:
        return _safe_hold(f"DeepSeek request failed: {error}")

    except (KeyError, IndexError, json.JSONDecodeError) as error:
        return _safe_hold(f"DeepSeek returned something unparseable: {error}")

    # --- shape ---------------------------------------------------
    signal = str(decision.get("signal", "")).upper().strip()

    if signal not in {"BUY", "SELL", "HOLD"}:
        return _safe_hold(f"Model returned an invalid signal: {signal!r}")

    decision["signal"] = signal

    try:
        score = int(round(float(decision.get("confidence_score", 0))))
    except (TypeError, ValueError):
        return _safe_hold("confidence_score was not a number")

    decision["confidence_score"] = max(0, min(100, score))

    decision.setdefault("logic", "")

    decision["applied_rules"] = [
        {
            "setup": rule.get("setup"),
            "penalty": rule.get("confidence_reduction_points"),
        }
        for rule in rules
    ]

    # --- the threshold -------------------------------------------
    # Applied AFTER the model has taken its learned-rule deductions,
    # so a penalised setup can genuinely fall below the bar.
    if (
        decision["signal"] in {"BUY", "SELL"}
        and decision["confidence_score"] < config.CONFIDENCE_THRESHOLD
    ):
        decision["logic"] = (
            f"Forced HOLD: confidence {decision['confidence_score']} is "
            f"below the {config.CONFIDENCE_THRESHOLD} threshold. "
            f"Model reasoning: {decision['logic']}"
        )
        decision["signal"] = "HOLD"
        decision["forced_hold"] = True

    return _validate_geometry(decision, market_data)
