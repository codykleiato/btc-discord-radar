import os
import json
import time
import urllib.request
import urllib.error
import threading
from datetime import datetime, timezone
from flask import Flask

# ============================================================
# PKLA BTC DISCORD RADAR
# Coinbase BTC-USD / 15M
# Discord delivery uses a BOT REST API, NOT a webhook.
# ============================================================

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "").strip()

SEND_NO_TRADE = os.environ.get("SEND_NO_TRADE", "0") == "1"
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "10"))

SYMBOL = "BTC-USD"
GRANULARITY = 900
COINBASE_URL = (
    "https://api.exchange.coinbase.com/products/"
    f"{SYMBOL}/candles?granularity={GRANULARITY}"
)

app = Flask(__name__)


@app.route("/")
def home():
    return {
        "status": "online",
        "service": "btc-discord-radar",
        "discord": "bot_api"
    }


@app.route("/test")
def test_discord():
    embed = {
        "title": "ðŸ§ª PKLA BTC Radar Test",
        "description": (
            "Discord connection successful!\n\n"
            "Render â†’ PKLA Bot â†’ Discord is working."
        ),
        "color": 5763719,
        "footer": {"text": "PKLA Signal Hub â€¢ Connection Test"},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    success, error_detail = send_discord(embed, return_error=True)

    if success:
        return {
            "status": "success",
            "message": "Test message sent to Discord"
        }

    return {
        "status": "error",
        "message": "Discord test failed",
        "error": error_detail
    }, 500


def start_web_server():
    port = int(os.environ.get("PORT", "10000"))
    print(f"Starting web server on port {port}...", flush=True)
    app.run(host="0.0.0.0", port=port)


def get_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PKLA-BTC-Radar/4.0",
            "Accept": "application/json"
        }
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def get_btc_candles():
    print("Requesting BTC candles from Coinbase...", flush=True)
    candles = get_json(COINBASE_URL)

    if not candles:
        raise RuntimeError("Coinbase returned no candles.")

    candles = sorted(candles, key=lambda candle: int(candle[0]))
    print(f"Coinbase returned {len(candles)} candles.", flush=True)
    return candles


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if not values:
        return []
    if len(values) < period:
        return [values[0]] * len(values)

    multiplier = 2.0 / (period + 1.0)
    result = [values[0]]

    for price in values[1:]:
        result.append(
            (price - result[-1]) * multiplier + result[-1]
        )
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
        return 100.0 if avg_gain else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values):
    ema12 = ema(values, 12)
    ema26 = ema(values, 26)

    macd_line = [a - b for a, b in zip(ema12, ema26)]
    signal_line = ema(macd_line, 9)

    if not macd_line:
        return 0.0, 0.0

    return macd_line[-1], signal_line[-1]


# ============================================================
# DISCORD BOT REST API
# ============================================================

def send_discord(embed, return_error=False):
    if not DISCORD_BOT_TOKEN:
        error = "DISCORD_BOT_TOKEN is missing or empty."
        print("Discord error:", error, flush=True)
        return (False, error) if return_error else False

    if not DISCORD_CHANNEL_ID:
        error = "DISCORD_CHANNEL_ID is missing or empty."
        print("Discord error:", error, flush=True)
        return (False, error) if return_error else False

    url = (
        "https://discord.com/api/v10/channels/"
        f"{DISCORD_CHANNEL_ID}/messages"
    )

    payload = {
        "username": "Cash Gang BTC Radar",
        "embeds": [embed]
    }

    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "PKLA-BTC-Radar/4.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = response.status
            print("Discord Bot API response:", status, flush=True)

            if 200 <= status < 300:
                return (True, None) if return_error else True

            error = f"Discord Bot API returned HTTP {status}"
            print("Discord error:", error, flush=True)
            return (False, error) if return_error else False

    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        detail = f"HTTP {error.code}: {body}"
        print("Discord error:", detail, flush=True)
        return (False, detail) if return_error else False

    except urllib.error.URLError as error:
        detail = f"URL error: {error.reason}"
        print("Discord error:", detail, flush=True)
        return (False, detail) if return_error else False

    except Exception as error:
        detail = f"{type(error).__name__}: {error}"
        print("Discord error:", detail, flush=True)
        return (False, detail) if return_error else False


# ============================================================
# BTC ANALYSIS
# ============================================================

def analyze_btc():
    candles = get_btc_candles()

    if len(candles) < 60:
        raise RuntimeError("Not enough Coinbase candles.")

    # Latest candle can still be forming.
    closed = candles[:-1]

    if len(closed) < 50:
        raise RuntimeError("Not enough closed candles.")

    opens = [float(c[3]) for c in closed]
    highs = [float(c[2]) for c in closed]
    lows = [float(c[1]) for c in closed]
    closes = [float(c[4]) for c in closed]
    volumes = [float(c[5]) for c in closed]
    candle_times = [int(c[0]) for c in closed]

    price = closes[-1]
    current_open = opens[-1]
    current_high = highs[-1]
    current_low = lows[-1]
    current_volume = volumes[-1]

    ema9 = ema(closes, 9)[-1]
    ema21 = ema(closes, 21)[-1]
    ema40 = ema(closes, 40)[-1]
    current_rsi = rsi(closes, 14)
    macd_value, macd_signal = macd(closes)

    volume_window = volumes[-21:-1]
    average_volume = (
        sum(volume_window) / len(volume_window)
        if volume_window else current_volume
    )
    relative_volume = (
        current_volume / average_volume
        if average_volume > 0 else 1.0
    )

    candle_range = max(current_high - current_low, 0.01)
    close_position = (price - current_low) / candle_range

    bullish_candle = price > current_open
    bearish_candle = price < current_open

    recent_change = price - closes[-4]
    bullish_momentum = recent_change > 0
    bearish_momentum = recent_change < 0

    previous_high = max(highs[-21:-1])
    previous_low = min(lows[-21:-1])

    breakout_up = price > previous_high
    breakout_down = price < previous_low

    bullish_score = 0
    bearish_score = 0
    reasons = []

    if ema9 > ema21:
        bullish_score += 2
        reasons.append("ðŸŸ¢ EMA9 > EMA21")
    else:
        bearish_score += 2
        reasons.append("ðŸ”´ EMA9 < EMA21")

    if price > ema40:
        bullish_score += 2
        reasons.append("ðŸŸ¢ BTC above EMA40")
    else:
        bearish_score += 2
        reasons.append("ðŸ”´ BTC below EMA40")

    if current_rsi >= 55:
        bullish_score += 2
        reasons.append(f"ðŸŸ¢ RSI bullish ({current_rsi:.1f})")
    elif current_rsi <= 45:
        bearish_score += 2
        reasons.append(f"ðŸ”´ RSI bearish ({current_rsi:.1f})")
    elif current_rsi > 50:
        bullish_score += 1
        reasons.append(f"ðŸŸ¢ RSI slightly bullish ({current_rsi:.1f})")
    else:
        bearish_score += 1
        reasons.append(f"ðŸ”´ RSI slightly bearish ({current_rsi:.1f})")

    if macd_value > macd_signal:
        bullish_score += 2
        reasons.append("ðŸŸ¢ MACD bullish")
    else:
        bearish_score += 2
        reasons.append("ðŸ”´ MACD bearish")

    if relative_volume >= 1.15:
        if bullish_candle:
            bullish_score += 2
            reasons.append(
                f"ðŸŸ¢ Strong bullish volume ({relative_volume:.2f}x)"
            )
        elif bearish_candle:
            bearish_score += 2
            reasons.append(
                f"ðŸ”´ Strong bearish volume ({relative_volume:.2f}x)"
            )
    elif relative_volume < 0.80:
        reasons.append(f"âšª Low volume ({relative_volume:.2f}x)")
    else:
        reasons.append(f"âšª Normal volume ({relative_volume:.2f}x)")

    if bullish_candle and close_position >= 0.70:
        bullish_score += 1
        reasons.append("ðŸŸ¢ Strong bullish candle close")
    elif bearish_candle and close_position <= 0.30:
        bearish_score += 1
        reasons.append("ðŸ”´ Strong bearish candle close")

    if bullish_momentum:
        bullish_score += 1
        reasons.append("ðŸŸ¢ Short-term momentum UP")
    elif bearish_momentum:
        bearish_score += 1
        reasons.append("ðŸ”´ Short-term momentum DOWN")

    if breakout_up:
        bullish_score += 3
        reasons.append("ðŸš€ Upside range breakout")
    elif breakout_down:
        bearish_score += 3
        reasons.append("ðŸ“‰ Downside range breakout")

    score_gap = abs(bullish_score - bearish_score)
    strongest_score = max(bullish_score, bearish_score)

    if bullish_score >= 8 and bullish_score > bearish_score:
        signal = "â¬†ï¸ BET UP"
        direction = "UP"
    elif bearish_score >= 8 and bearish_score > bullish_score:
        signal = "â¬‡ï¸ BET DOWN"
        direction = "DOWN"
    else:
        signal = "â¸ï¸ NO TRADE"
        direction = "NONE"

    if direction in ("UP", "DOWN"):
        confidence = (
            50
            + score_gap * 5
            + max(0, strongest_score - 8) * 3
        )
    else:
        confidence = 50

    confidence = int(max(50, min(95, confidence)))
    hold_candles = 2 if confidence >= 80 else 1

    recent_ranges = [
        highs[i] - lows[i]
        for i in range(max(0, len(highs) - 14), len(highs))
    ]
    average_range = (
        sum(recent_ranges) / len(recent_ranges)
        if recent_ranges else candle_range
    )
    if average_range <= 0:
        average_range = candle_range

    if direction == "UP":
        entry = price
        take_profit = price + average_range * 1.30
        stop_loss = price - average_range * 0.90
    elif direction == "DOWN":
        entry = price
        take_profit = price - average_range * 1.30
        stop_loss = price + average_range * 0.90
    else:
        entry = price
        take_profit = price
        stop_loss = price

    candle_timestamp = candle_times[-1]
    candle_datetime = datetime.fromtimestamp(
        candle_timestamp, tz=timezone.utc
    )

    return {
        "price": price,
        "signal": signal,
        "direction": direction,
        "confidence": confidence,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "score_gap": score_gap,
        "rsi": current_rsi,
        "macd": macd_value,
        "macd_signal": macd_signal,
        "ema9": ema9,
        "ema21": ema21,
        "ema40": ema40,
        "relative_volume": relative_volume,
        "previous_high": previous_high,
        "previous_low": previous_low,
        "entry": entry,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "hold_candles": hold_candles,
        "reasons": reasons,
        "candle_timestamp": candle_timestamp,
        "candle_datetime": candle_datetime
    }


def build_embed(result):
    direction = result["direction"]

    if direction == "UP":
        title = "ðŸŸ¢ BTC UP SIGNAL"
        color = 5763719
    elif direction == "DOWN":
        title = "ðŸ”´ BTC DOWN SIGNAL"
        color = 15548997
    else:
        title = "âšª BTC NO TRADE"
        color = 9807270

    reasons_text = "\n".join(result["reasons"])

    return {
        "title": title,
        "description": (
            "**PKLA BTC 15-Minute Market Radar**\n"
            "Automated technical analysis"
        ),
        "color": color,
        "fields": [
            {
                "name": "â‚¿ BTC PRICE",
                "value": f"**${result['price']:,.2f}**",
                "inline": False
            },
            {
                "name": "ðŸ“Š SIGNAL",
                "value": f"**{result['signal']}**",
                "inline": True
            },
            {
                "name": "ðŸŽ¯ CONFIDENCE",
                "value": f"**{result['confidence']}%**",
                "inline": True
            },
            {
                "name": "ðŸ•¯ HOLD",
                "value": f"**{result['hold_candles']} candle(s)**",
                "inline": True
            },
            {
                "name": "ðŸ“ˆ SCORE",
                "value": (
                    f"Bullish: **{result['bullish_score']}**\n"
                    f"Bearish: **{result['bearish_score']}**\n"
                    f"Gap: **{result['score_gap']}**"
                ),
                "inline": True
            },
            {
                "name": "ðŸ“ INDICATORS",
                "value": (
                    f"RSI: **{result['rsi']:.1f}**\n"
                    f"MACD: **{result['macd']:.2f}**\n"
                    f"Signal: **{result['macd_signal']:.2f}**\n"
                    f"EMA9: **${result['ema9']:,.2f}**\n"
                    f"EMA21: **${result['ema21']:,.2f}**\n"
                    f"EMA40: **${result['ema40']:,.2f}**"
                ),
                "inline": False
            },
            {
                "name": "ðŸ“ TRADE LEVELS",
                "value": (
                    f"Entry: **${result['entry']:,.2f}**\n"
                    f"Take Profit: **${result['take_profit']:,.2f}**\n"
                    f"Stop Loss: **${result['stop_loss']:,.2f}**"
                ),
                "inline": False
            },
            {
                "name": "ðŸ”¬ ANALYSIS",
                "value": reasons_text,
                "inline": False
            },
            {
                "name": "ðŸ“Š RANGE",
                "value": (
                    f"Previous High: **${result['previous_high']:,.2f}**\n"
                    f"Previous Low: **${result['previous_low']:,.2f}**\n"
                    f"Relative Volume: **{result['relative_volume']:.2f}x**"
                ),
                "inline": False
            }
        ],
        "footer": {
            "text": "PKLA Signal Hub â€¢ BTC â€¢ RSI â€¢ MACD â€¢ EMA â€¢ Volume"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def send_signal(result):
    if result["direction"] == "NONE" and not SEND_NO_TRADE:
        print(
            "NO TRADE signal - Discord message skipped.",
            flush=True
        )
        return

    print("Sending signal to Discord Bot API...", flush=True)

    success = send_discord(build_embed(result))

    if success:
        print(
            "Signal successfully sent to Discord.",
            flush=True
        )
    else:
        print(
            "Signal was NOT sent to Discord.",
            flush=True
        )


def run_radar():
    print("==========================================", flush=True)
    print("       PKLA BTC DISCORD RADAR", flush=True)
    print("==========================================", flush=True)
    print("Data: Coinbase Exchange", flush=True)
    print("Market: BTC-USD", flush=True)
    print("Timeframe: 15M", flush=True)
    print("TradingView webhook: NOT REQUIRED", flush=True)
    print("Discord: BOT API", flush=True)
    print(f"Check interval: {CHECK_INTERVAL}s", flush=True)
    print("==========================================", flush=True)

    if DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID:
        print("Discord bot: CONFIGURED", flush=True)
    else:
        print(
            "WARNING: DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID "
            "is not configured.",
            flush=True
        )

    print("Radar thread started.", flush=True)

    last_processed_candle = None

    while True:
        try:
            print(
                "Checking Coinbase for new closed candle...",
                flush=True
            )

            candles = get_btc_candles()

            if len(candles) < 3:
                print("Not enough Coinbase data.", flush=True)
                time.sleep(CHECK_INTERVAL)
                continue

            latest_closed = candles[-2]
            candle_timestamp = int(latest_closed[0])
            candle_datetime = datetime.fromtimestamp(
                candle_timestamp, tz=timezone.utc
            )

            print(
                "Latest closed candle:",
                candle_datetime.isoformat(),
                flush=True
            )

            if last_processed_candle == candle_timestamp:
                print(
                    "No new closed 15M candle yet.",
                    flush=True
                )
                time.sleep(CHECK_INTERVAL)
                continue

            print("New closed 15M candle detected!", flush=True)
            print("Analyzing BTC...", flush=True)

            result = analyze_btc()

            print(
                "Price:",
                f"${result['price']:,.2f}",
                flush=True
            )
            print("Signal:", result["signal"], flush=True)
            print(
                "Confidence:",
                f"{result['confidence']}%",
                flush=True
            )
            print(
                "Bullish score:",
                result["bullish_score"],
                flush=True
            )
            print(
                "Bearish score:",
                result["bearish_score"],
                flush=True
            )
            print(
                "Candle analyzed:",
                result["candle_datetime"].isoformat(),
                flush=True
            )

            send_signal(result)
            last_processed_candle = candle_timestamp

            print("Candle processing complete.", flush=True)

        except urllib.error.HTTPError as error:
            print("HTTP error:", error, flush=True)
        except urllib.error.URLError as error:
            print("Network error:", error, flush=True)
        except Exception as error:
            print("Radar error:", repr(error), flush=True)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    print("Starting PKLA BTC Discord Radar...", flush=True)

    threading.Thread(
        target=start_web_server,
        daemon=True
    ).start()

    run_radar()