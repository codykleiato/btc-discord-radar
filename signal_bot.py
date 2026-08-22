import os
import time
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

# ============================================================
# PKLA BTC DISCORD RADAR
# Sends ONE signal after EVERY completed 15-minute BTC candle.
#
# Required environment variable:
#   DISCORD_WEBHOOK_URL
#
# Optional:
#   COINBASE_PRODUCT=BTC-USD
#   POLL_SECONDS=10
#   PORT=10000
# ============================================================

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
COINBASE_PRODUCT = os.getenv("COINBASE_PRODUCT", "BTC-USD").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
PORT = int(os.getenv("PORT", "10000"))

COINBASE_URL = (
    "https://api.exchange.coinbase.com/products/"
    f"{COINBASE_PRODUCT}/candles"
)

app = Flask(__name__)

last_sent_candle = None
last_signal = None


def get_candles(limit=120):
    """Get 15-minute candles from Coinbase."""
    params = {"granularity": 900}
    response = requests.get(
        COINBASE_URL,
        params=params,
        timeout=15,
        headers={"User-Agent": "PKLA-BTC-Radar/1.0"},
    )
    response.raise_for_status()

    # Coinbase returns: [time, low, high, open, close, volume]
    raw = response.json()

    candles = []
    for row in raw:
        candles.append(
            {
                "time": int(row[0]),
                "low": float(row[1]),
                "high": float(row[2]),
                "open": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
        )

    candles.sort(key=lambda x: x["time"])
    return candles[-limit:]


def ema(values, period):
    if not values:
        return []

    alpha = 2.0 / (period + 1.0)
    result = [values[0]]

    for value in values[1:]:
        result.append((value * alpha) + (result[-1] * (1.0 - alpha)))

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        return max(highs) - min(lows)

    trs = []
    for i, candle in enumerate(candles):
        if i == 0:
            tr = candle["high"] - candle["low"]
        else:
            previous_close = candles[i - 1]["close"]
            tr = max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close),
            )
        trs.append(tr)

    return sum(trs[-period:]) / period


def macd(values):
    ema12 = ema(values, 12)
    ema26 = ema(values, 26)
    line = [a - b for a, b in zip(ema12, ema26)]
    signal = ema(line, 9)
    return line[-1], signal[-1]


def calculate_signal(candles):
    closes = [c["close"] for c in candles]

    ema9 = ema(closes, 9)[-1]
    ema21 = ema(closes, 21)[-1]
    ema40 = ema(closes, 40)[-1]

    current_rsi = rsi(closes, 14)
    macd_line, macd_signal = macd(closes)
    current_atr = max(atr(candles, 14), 0.01)

    entry = closes[-1]

    bullish_points = 0
    bearish_points = 0

    if ema9 > ema21:
        bullish_points += 1
    else:
        bearish_points += 1

    if entry > ema40:
        bullish_points += 1
    else:
        bearish_points += 1

    if current_rsi >= 50:
        bullish_points += 1
    else:
        bearish_points += 1

    if macd_line > macd_signal:
        bullish_points += 1
    else:
        bearish_points += 1

    if entry > ema21:
        bullish_points += 1
    else:
        bearish_points += 1

    if bullish_points > bearish_points:
        direction = "UP"
        score_bull = bullish_points
        score_bear = bearish_points
        gap = bullish_points - bearish_points
    elif bearish_points > bullish_points:
        direction = "DOWN"
        score_bull = bullish_points
        score_bear = bearish_points
        gap = bearish_points - bullish_points
    else:
        # Force a direction so every candle produces a trade callout.
        direction = "UP" if macd_line >= macd_signal else "DOWN"
        score_bull = bullish_points
        score_bear = bearish_points
        gap = 0

    # Confidence is intentionally capped. This is a technical score,
    # not a guarantee of the next candle's result.
    confidence = min(100, 60 + (gap * 8))

    if direction == "UP":
        take_profit = entry + (current_atr * 0.75)
        stop_loss = entry - (current_atr * 0.55)
        target = entry + (current_atr * 0.60)
    else:
        take_profit = entry - (current_atr * 0.75)
        stop_loss = entry + (current_atr * 0.55)
        target = entry - (current_atr * 0.60)

    return {
        "direction": direction,
        "entry": entry,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "target": target,
        "target_difference": abs(target - entry),
        "confidence": confidence,
        "bullish": score_bull,
        "bearish": score_bear,
        "gap": gap,
        "rsi": current_rsi,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "ema9": ema9,
        "ema21": ema21,
        "ema40": ema40,
        "atr": current_atr,
    }


def fmt_price(value):
    return f"${value:,.2f}"


def candle_window(timestamp):
    start = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    end = datetime.fromtimestamp(timestamp + 900, tz=timezone.utc)
    return start, end


def build_discord_payload(candle, signal):
    start, end = candle_window(candle["time"])
    color = 0x22C55E if signal["direction"] == "UP" else 0xEF4444
    emoji = "🟢" if signal["direction"] == "UP" else "🔴"

    hold_candles = 2
    hold_minutes = hold_candles * 15

    analysis = []
    if signal["ema9"] > signal["ema21"]:
        analysis.append("🟢 EMA9 > EMA21")
    else:
        analysis.append("🔴 EMA9 < EMA21")

    if candle["close"] > signal["ema40"]:
        analysis.append("🟢 BTC above EMA40")
    else:
        analysis.append("🔴 BTC below EMA40")

    if signal["rsi"] >= 50:
        analysis.append(f"🟢 RSI bullish ({signal['rsi']:.1f})")
    else:
        analysis.append(f"🔴 RSI bearish ({signal['rsi']:.1f})")

    if signal["macd"] > signal["macd_signal"]:
        analysis.append("🟢 MACD bullish")
    else:
        analysis.append("🔴 MACD bearish")

    if signal["direction"] == "UP":
        above_target = candle["close"] < signal["target"]
        analysis.append(
            "🟢 BTC below 15m target"
            if above_target
            else "🔴 BTC at/above 15m target"
        )
    else:
        below_target = candle["close"] > signal["target"]
        analysis.append(
            "🟢 BTC above 15m target"
            if below_target
            else "🔴 BTC at/below 15m target"
        )

    description = (
        f"**PKLA BTC 15-Minute Market Radar**\n"
        f"Coinbase {COINBASE_PRODUCT} technical analysis\n\n"
        f"💲 **BTC PRICE**\n"
        f"{fmt_price(candle['close'])}\n\n"
        f"📊 **SIGNAL**\n"
        f"**BET {signal['direction']}**\n\n"
        f"🎯 **CONFIDENCE**\n"
        f"**{signal['confidence']}%**\n\n"
        f"🕯️ **HOLD**\n"
        f"**{hold_candles} candle(s) / up to {hold_minutes} minutes**\n\n"
        f"📈 **SCORE**\n"
        f"Bullish: **{signal['bullish']}**\n"
        f"Bearish: **{signal['bearish']}**\n"
        f"Gap: **+{signal['gap']}**\n\n"
        f"📐 **INDICATORS**\n"
        f"RSI: **{signal['rsi']:.1f}**\n"
        f"MACD: **{signal['macd']:.2f}**\n"
        f"Signal: **{signal['macd_signal']:.2f}**\n"
        f"EMA9: **{fmt_price(signal['ema9'])}**\n"
        f"EMA21: **{fmt_price(signal['ema21'])}**\n"
        f"EMA40: **{fmt_price(signal['ema40'])}**\n\n"
        f"📍 **TRADE LEVELS**\n"
        f"Entry: **{fmt_price(signal['entry'])}**\n"
        f"Take Profit: **{fmt_price(signal['take_profit'])}**\n"
        f"Stop Loss: **{fmt_price(signal['stop_loss'])}**\n\n"
        f"🎯 **15-MINUTE TARGET**\n"
        f"Target: **{fmt_price(signal['target'])}**\n"
        f"Difference: **${signal['target_difference']:,.2f}**\n"
        f"Window: **{end.strftime('%H:%M UTC')}**\n\n"
        f"🔬 **ANALYSIS**\n"
        + "\n".join(analysis)
        + f"\n\n_Candle closed: {end.strftime('%Y-%m-%d %H:%M UTC')}_"
    )

    return {
        "username": "PKLA BTC Radar",
        "embeds": [
            {
                "title": f"{emoji} BTC {signal['direction']} SIGNAL",
                "description": description,
                "color": color,
                "footer": {
                    "text": "PKLA BTC Radar • New signal every completed 15-minute candle"
                },
                "timestamp": end.isoformat(),
            }
        ],
    }


def send_to_discord(payload):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL is not set. Add it to your Render environment variables."
        )

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json=payload,
        timeout=15,
    )
    response.raise_for_status()


def process_latest_closed_candle():
    global last_sent_candle, last_signal

    candles = get_candles(120)
    if len(candles) < 50:
        raise RuntimeError("Not enough candle history returned by Coinbase.")

    # Coinbase can include the currently forming candle. The candle immediately
    # before it is the latest completed 15-minute candle.
    latest_closed = candles[-2]
    candle_id = latest_closed["time"]

    if last_sent_candle == candle_id:
        return False

    # Use candles only through the completed candle for all calculations.
    closed_history = candles[:-1]
    signal = calculate_signal(closed_history)

    payload = build_discord_payload(latest_closed, signal)
    send_to_discord(payload)

    last_sent_candle = candle_id
    last_signal = signal

    closed_at = datetime.fromtimestamp(
        candle_id + 900, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

    print(
        f"[SENT] {closed_at} | BTC {signal['direction']} | "
        f"entry={fmt_price(signal['entry'])} | "
        f"confidence={signal['confidence']}%"
    )

    return True


def radar_loop():
    print("=" * 50)
    print("       PKLA BTC DISCORD RADAR")
    print("=" * 50)
    print(f"Data: Coinbase {COINBASE_PRODUCT}")
    print("Timeframe: 15M")
    print("TradingView webhook: NOT REQUIRED")
    print("Discord: ENABLED")
    print("=" * 50)

    if DISCORD_WEBHOOK_URL:
        print("Discord webhook: CONFIGURED")
    else:
        print("Discord webhook: MISSING")

    print("Radar thread started.")

    while True:
        try:
            process_latest_closed_candle()
        except Exception as exc:
            print(f"[ERROR] {type(exc).__name__}: {exc}")

        time.sleep(POLL_SECONDS)


@app.get("/")
def home():
    return jsonify(
        {
            "status": "online",
            "bot": "PKLA BTC Discord Radar",
            "product": COINBASE_PRODUCT,
            "timeframe": "15m",
            "last_sent_candle": last_sent_candle,
            "last_signal": last_signal,
        }
    )


@app.get("/health")
def health():
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    thread = threading.Thread(target=radar_loop, daemon=True)
    thread.start()

    print(f"Starting web server on port {PORT}...")
    app.run(host="0.0.0.0", port=PORT, debug=False)