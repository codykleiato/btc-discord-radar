import os
import time
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

SYMBOL = "BTC-USD"
INTERVAL = "15m"
CANDLE_LIMIT = 120
SEND_NO_TRADE = os.getenv("SEND_NO_TRADE", "true").lower() == "true"

last_radar_candle_time = None
last_radar_sent_at = None
last_radar_error = None


def money(value):
    return f"${value:,.2f}"


def ema(values, length):
    if len(values) < length:
        return None

    multiplier = 2 / (length + 1)
    value = values[0]

    for price in values[1:]:
        value = (price - value) * multiplier + value

    return value


def rsi(values, length=14):
    if len(values) < length + 1:
        return None

    gains = []
    losses = []

    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    average_gain = sum(gains[-length:]) / length
    average_loss = sum(losses[-length:]) / length

    if average_loss == 0:
        return 100.0

    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def macd(values, fast_length=12, slow_length=26, signal_length=9):
    if len(values) < slow_length + signal_length:
        return None, None, None

    macd_history = []

    for endpoint in range(slow_length, len(values) + 1):
        subset = values[:endpoint]
        fast_ema = ema(subset[-fast_length:], fast_length)
        slow_ema = ema(subset[-slow_length:], slow_length)

        if fast_ema is not None and slow_ema is not None:
            macd_history.append(fast_ema - slow_ema)

    if len(macd_history) < signal_length:
        return None, None, None

    macd_line = macd_history[-1]
    signal_line = ema(macd_history[-signal_length:], signal_length)
    histogram = macd_line - signal_line

    return macd_line, signal_line, histogram


def get_coinbase_candles():
    now = int(time.time())
    start = now - (CANDLE_LIMIT * 15 * 60)

    url = (
        "https://api.coinbase.com/api/v3/brokerage/market/"
        "products/BTC-USD/candles"
    )

    params = {
        "start": str(start),
        "end": str(now),
        "granularity": "FIFTEEN_MINUTE",
    }

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()

    payload = response.json()
    raw_candles = payload.get("candles", [])

    if not raw_candles:
        raise ValueError("Coinbase returned no BTC-USD candles.")

    candles = []

    for candle in raw_candles:
        candles.append(
            {
                "open_time": int(candle["start"]) * 1000,
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "volume": float(candle["volume"]),
            }
        )

    return sorted(candles, key=lambda candle: candle["open_time"])


def build_radar_embed(candles):
    # Coinbase normally includes the current unfinished 15-minute candle last.
    # Ignore it so each Discord post uses finalized candle data.
    closed_candles = candles[:-1]

    if len(closed_candles) < 50:
        raise ValueError("Not enough completed Coinbase 15-minute candles.")

    latest = closed_candles[-1]
    closes = [candle["close"] for candle in closed_candles]
    volumes = [candle["volume"] for candle in closed_candles]

    price = latest["close"]
    rsi_value = rsi(closes, 14)
    ema9 = ema(closes[-9:], 9)
    ema21 = ema(closes[-21:], 21)
    ema40 = ema(closes[-40:], 40)

    macd_value, macd_signal, macd_histogram = macd(closes)

    average_volume = sum(volumes[-21:-1]) / 20
    volume_ratio = latest["volume"] / average_volume if average_volume else 1

    bullish = 0
    bearish = 0
    analysis = []

    if ema9 > ema21:
        bullish += 1
        analysis.append("🟢 EMA9 > EMA21")
    else:
        bearish += 1
        analysis.append("🔴 EMA9 < EMA21")

    if price > ema40:
        bullish += 1
        analysis.append("🟢 BTC above EMA40")
    else:
        bearish += 1
        analysis.append("🔴 BTC below EMA40")

    if 55 <= rsi_value < 75:
        bullish += 1
        analysis.append(f"🟢 RSI bullish ({rsi_value:.1f})")
    elif 25 < rsi_value <= 45:
        bearish += 1
        analysis.append(f"🔴 RSI bearish ({rsi_value:.1f})")
    elif rsi_value >= 75:
        bearish += 1
        analysis.append(f"🟡 RSI overbought ({rsi_value:.1f})")
    elif rsi_value <= 25:
        bullish += 1
        analysis.append(f"🟡 RSI oversold ({rsi_value:.1f})")
    else:
        analysis.append(f"⚪ RSI neutral ({rsi_value:.1f})")

    if macd_value > macd_signal:
        bullish += 1
        analysis.append("🟢 MACD bullish")
    else:
        bearish += 1
        analysis.append("🔴 MACD bearish")

    if macd_histogram > 0:
        bullish += 1
    else:
        bearish += 1

    if volume_ratio >= 1.25:
        if bullish >= bearish:
            bullish += 1
            analysis.append(f"🟢 Strong bullish volume ({volume_ratio:.2f}x)")
        else:
            bearish += 1
            analysis.append(f"🔴 Strong bearish volume ({volume_ratio:.2f}x)")
    else:
        analysis.append(f"⚪ Normal volume ({volume_ratio:.2f}x)")

    gap = bullish - bearish
    confidence = min(95, max(50, 50 + abs(gap) * 9))

    if gap >= 2:
        signal = "BET UP"
        title = "🟢 BTC UP SIGNAL"
        color = 0x2ECC71
        hold_candles = 2
        take_profit = price * 1.0058
        stop_loss = price * 0.9960
    elif gap <= -2:
        signal = "BET DOWN"
        title = "🔴 BTC DOWN SIGNAL"
        color = 0xE74C3C
        hold_candles = 2
        take_profit = price * 0.9942
        stop_loss = price * 1.0040
    else:
        signal = "NO TRADE"
        title = "🟡 BTC WAIT SIGNAL"
        color = 0xF1C40F
        hold_candles = 1
        take_profit = price
        stop_loss = price

    candle_time = datetime.fromtimestamp(
        latest["open_time"] / 1000,
        tz=timezone.utc,
    ).strftime("%b %d, %Y • %I:%M %p UTC")

    description = (
        "**PKLA BTC 15-Minute Market Radar**\n"
        "Coinbase BTC-USD technical analysis\n\n"
        "₿ **BTC PRICE**\n"
        f"**{money(price)}**\n\n"
        "📊 **SIGNAL**\n"
        f"**{signal}**\n\n"
        "🎯 **CONFIDENCE**\n"
        f"**{confidence}%**\n\n"
        "🕯️ **HOLD**\n"
        f"**{hold_candles} candle(s)**\n\n"
        "📈 **SCORE**\n"
        f"Bullish: **{bullish}**\n"
        f"Bearish: **{bearish}**\n"
        f"Gap: **{gap:+d}**\n\n"
        "📐 **INDICATORS**\n"
        f"RSI: **{rsi_value:.1f}**\n"
        f"MACD: **{macd_value:.2f}**\n"
        f"Signal: **{macd_signal:.2f}**\n"
        f"EMA9: **{money(ema9)}**\n"
        f"EMA21: **{money(ema21)}**\n"
        f"EMA40: **{money(ema40)}**\n\n"
        "📍 **TRADE LEVELS**\n"
        f"Entry: **{money(price)}**\n"
        f"Take Profit: **{money(take_profit)}**\n"
        f"Stop Loss: **{money(stop_loss)}**\n\n"
        "🔬 **ANALYSIS**\n"
        + "\n".join(analysis)
    )

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "footer": {
            "text": f"{SYMBOL} • Confirmed 15-minute candle • {candle_time}"
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return embed, latest["open_time"], signal


def post_to_discord(embed):
    if not DISCORD_WEBHOOK_URL:
        raise ValueError(
            "DISCORD_WEBHOOK_URL is missing in Render Environment."
        )

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"embeds": [embed]},
        timeout=20,
    )
    response.raise_for_status()


def radar_loop():
    global last_radar_candle_time, last_radar_sent_at, last_radar_error

    time.sleep(10)

    while True:
        try:
            candles = get_coinbase_candles()
            embed, candle_time, signal = build_radar_embed(candles)

            new_closed_candle = candle_time != last_radar_candle_time

            if signal == "NO TRADE" and not SEND_NO_TRADE:
                new_closed_candle = False

            if new_closed_candle:
                post_to_discord(embed)
                last_radar_candle_time = candle_time
                last_radar_sent_at = datetime.now(timezone.utc).isoformat()
                last_radar_error = None

                print(
                    f"Radar sent: {signal} for candle {candle_time}",
                    flush=True,
                )

        except Exception as error:
            last_radar_error = str(error)
            print(f"Radar error: {error}", flush=True)

        # Poll each minute; post at most once for each new 15-minute close.
        time.sleep(60)


@app.get("/")
def home():
    return jsonify(
        {
            "service": "btc-discord-radar",
            "status": "running",
            "source": "Coinbase BTC-USD",
            "timeframe": INTERVAL,
            "last_radar_sent_at": last_radar_sent_at,
            "last_radar_error": last_radar_error,
        }
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


# Optional: preserves the old endpoint if you ever use webhooks later.
@app.post("/webhook")
def webhook():
    payload = request.get_json(silent=True) or {}

    supplied_secret = str(payload.get("secret", "")).strip()

    if WEBHOOK_SECRET and supplied_secret != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    signal = str(payload.get("signal", "SIGNAL")).upper()
    ticker = str(payload.get("ticker", SYMBOL))
    price = str(payload.get("price", "N/A"))
    timeframe = str(payload.get("timeframe", "N/A"))

    is_up = any(word in signal for word in ["BUY", "UP", "LONG"])
    color = 0x2ECC71 if is_up else 0xE74C3C

    embed = {
        "title": f"{'🟢' if is_up else '🔴'} {ticker} {signal}",
        "description": (
            f"**Price:** {price}\n"
            f"**Timeframe:** {timeframe}"
        ),
        "color": color,
        "footer": {"text": "External webhook signal"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        post_to_discord(embed)
        return jsonify({"ok": True, "message": "Discord signal sent"})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


if __name__ == "__main__":
    threading.Thread(target=radar_loop, daemon=True).start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)