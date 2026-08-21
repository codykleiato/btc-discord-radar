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


def round_price(value):
    return f"${value:,.2f}"


def ema(values, length):
    if len(values) < length:
        return None

    multiplier = 2 / (length + 1)
    result = values[0]

    for value in values[1:]:
        result = (value - result) * multiplier + result

    return result


def rsi(values, length=14):
    if len(values) < length + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    average_gain = sum(gains[-length:]) / length
    average_loss = sum(losses[-length:]) / length

    if average_loss == 0:
        return 100.0

    rs = average_gain / average_loss
    return 100 - (100 / (1 + rs))


def macd(values, fast_length=12, slow_length=26, signal_length=9):
    if len(values) < slow_length + signal_length:
        return None, None, None

    macd_values = []

    for end in range(slow_length, len(values) + 1):
        subset = values[:end]
        fast_ema = ema(subset[-fast_length:], fast_length)
        slow_ema = ema(subset[-slow_length:], slow_length)

        if fast_ema is not None and slow_ema is not None:
            macd_values.append(fast_ema - slow_ema)

    if len(macd_values) < signal_length:
        return None, None, None

    macd_line = macd_values[-1]
    signal_line = ema(macd_values[-signal_length:], signal_length)
    histogram = macd_line - signal_line

    return macd_line, signal_line, histogram


def get_btc_candles():
    end_time = int(time.time())
    start_time = end_time - (CANDLE_LIMIT * 15 * 60)

    url = (
        "https://api.coinbase.com/api/v3/brokerage/market/"
        "products/BTC-USD/candles"
    )

    params = {
        "start": str(start_time),
        "end": str(end_time),
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

    return sorted(candles, key=lambda item: item["open_time"])


def build_radar_embed(candles):
    closed_candles = candles[:-1]

    if len(closed_candles) < 50:
        raise ValueError("Not enough completed 15-minute candles yet.")

    latest = closed_candles[-1]
    closes = [item["close"] for item in closed_candles]
    volumes = [item["volume"] for item in closed_candles]

    current_price = latest["close"]
    current_rsi = rsi(closes, 14)
    ema9 = ema(closes[-9:], 9)
    ema21 = ema(closes[-21:], 21)
    ema40 = ema(closes[-40:], 40)

    macd_line, macd_signal, macd_histogram = macd(closes)

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

    if current_price > ema40:
        bullish += 1
        analysis.append("🟢 BTC above EMA40")
    else:
        bearish += 1
        analysis.append("🔴 BTC below EMA40")

    if 55 <= current_rsi < 75:
        bullish += 1
        analysis.append(f"🟢 RSI bullish ({current_rsi:.1f})")
    elif 25 < current_rsi <= 45:
        bearish += 1
        analysis.append(f"🔴 RSI bearish ({current_rsi:.1f})")
    elif current_rsi >= 75:
        bearish += 1
        analysis.append(f"🟡 RSI overbought ({current_rsi:.1f})")
    elif current_rsi <= 25:
        bullish += 1
        analysis.append(f"🟡 RSI oversold ({current_rsi:.1f})")
    else:
        analysis.append(f"⚪ RSI neutral ({current_rsi:.1f})")

    if macd_line > macd_signal:
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
        entry = current_price
        take_profit = current_price * 1.0058
        stop_loss = current_price * 0.9960
    elif gap <= -2:
        signal = "BET DOWN"
        title = "🔴 BTC DOWN SIGNAL"
        color = 0xE74C3C
        hold_candles = 2
        entry = current_price
        take_profit = current_price * 0.9942
        stop_loss = current_price * 1.0040
    else:
        signal = "NO TRADE"
        title = "🟡 BTC WAIT SIGNAL"
        color = 0xF1C40F
        hold_candles = 1
        entry = current_price
        take_profit = current_price
        stop_loss = current_price

    candle_time = datetime.fromtimestamp(
        latest["open_time"] / 1000,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %I:%M %p UTC")

    description = (
        "**PKLA BTC 15-Minute Market Radar**\n"
        "Coinbase technical analysis\n\n"
        "₿ **BTC PRICE**\n"
        f"**{round_price(current_price)}**\n\n"
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
        f"RSI: **{current_rsi:.1f}**\n"
        f"MACD: **{macd_line:.2f}**\n"
        f"Signal: **{macd_signal:.2f}**\n"
        f"EMA9: **{round_price(ema9)}**\n"
        f"EMA21: **{round_price(ema21)}**\n"
        f"EMA40: **{round_price(ema40)}**\n\n"
        "📍 **TRADE LEVELS**\n"
        f"Entry: **{round_price(entry)}**\n"
        f"Take Profit: **{round_price(take_profit)}**\n"
        f"Stop Loss: **{round_price(stop_loss)}**\n\n"
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


def send_discord_embed(embed):
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

    time.sleep(15)

    while True:
        try:
            candles = get_btc_candles()
            embed, candle_time, signal = build_radar_embed(candles)

            should_send = candle_time != last_radar_candle_time

            if signal == "NO TRADE" and not SEND_NO_TRADE:
                should_send = False

            if should_send:
                send_discord_embed(embed)
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

        time.sleep(60)


@app.get("/")
def home():
    return jsonify(
        {
            "service": "btc-discord-radar",
            "status": "running",
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "last_radar_sent_at": last_radar_sent_at,
            "last_radar_error": last_radar_error,
        }
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


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
    event_time = str(
        payload.get("time", datetime.now(timezone.utc).isoformat())
    )

    is_buy = any(word in signal for word in ["BUY", "UP", "LONG"])
    color = 0x2ECC71 if is_buy else 0xE74C3C

    embed = {
        "title": f"{'🟢' if is_buy else '🔴'} {ticker} {signal}",
        "description": (
            f"**Price:** {price}\n"
            f"**Timeframe:** {timeframe}\n"
            f"**Time:** {event_time}"
        ),
        "color": color,
        "footer": {"text": "Webhook signal"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        send_discord_embed(embed)
        return jsonify({"ok": True, "message": "Discord signal sent"})
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 500


if __name__ == "__main__":
    threading.Thread(target=radar_loop, daemon=True).start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)