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
#
# Sends ONE Discord message for EVERY closed 15-minute candle.
# Includes:
#   ⬆️ BET UP
#   ⬇️ BET DOWN
#   ⏸️ NO TRADE
#
# Discord delivery uses BOT REST API.
# ============================================================


DISCORD_BOT_TOKEN = os.environ.get(
    "DISCORD_BOT_TOKEN", ""
).strip()

DISCORD_CHANNEL_ID = os.environ.get(
    "DISCORD_CHANNEL_ID", ""
).strip()

# ALWAYS send the result, including NO TRADE.
SEND_NO_TRADE = True

# How often the bot checks Coinbase for a new closed candle.
# This does NOT change the trading timeframe.
# The timeframe remains 15 minutes.
CHECK_INTERVAL = int(
    os.environ.get("CHECK_INTERVAL", "10")
)

SYMBOL = "BTC-USD"

# Coinbase 900 seconds = 15 minutes.
GRANULARITY = 900

COINBASE_URL = (
    "https://api.exchange.coinbase.com/products/"
    f"{SYMBOL}/candles?granularity={GRANULARITY}"
)


app = Flask(__name__)


# ============================================================
# WEB SERVER
# ============================================================

@app.route("/")
def home():
    return {
        "status": "online",
        "service": "btc-discord-radar",
        "discord": "bot_api",
        "timeframe": "15M",
        "send_every_15m": True
    }


@app.route("/test")
def test_discord():
    embed = {
        "title": "🧪 PKLA BTC Radar Test",
        "description": (
            "Discord connection successful!\n\n"
            "Render → PKLA Bot → Discord is working."
        ),
        "color": 5763719,
        "footer": {
            "text": "PKLA Signal Hub • Connection Test"
        },
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat()
    }

    success, error_detail = send_discord(
        embed,
        return_error=True
    )

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
    port = int(
        os.environ.get("PORT", "10000")
    )

    print(
        f"Starting web server on port {port}...",
        flush=True
    )

    app.run(
        host="0.0.0.0",
        port=port
    )


# ============================================================
# HTTP / COINBASE
# ============================================================

def get_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PKLA-BTC-Radar/5.0",
            "Accept": "application/json"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=15
    ) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def get_btc_candles():
    print(
        "Requesting BTC candles from Coinbase...",
        flush=True
    )

    candles = get_json(
        COINBASE_URL
    )

    if not candles:
        raise RuntimeError(
            "Coinbase returned no candles."
        )

    candles = sorted(
        candles,
        key=lambda candle: int(candle[0])
    )

    print(
        f"Coinbase returned {len(candles)} candles.",
        flush=True
    )

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
            (price - result[-1])
            * multiplier
            + result[-1]
        )

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = (
            values[i]
            - values[i - 1]
        )

        gains.append(
            max(change, 0.0)
        )

        losses.append(
            max(-change, 0.0)
        )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    for i in range(
        period,
        len(gains)
    ):
        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + gains[i]
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + losses[i]
        ) / period

    if avg_loss == 0:
        return (
            100.0
            if avg_gain
            else 50.0
        )

    rs = avg_gain / avg_loss

    return (
        100.0
        - (
            100.0
            / (1.0 + rs)
        )
    )


def macd(values):
    ema12 = ema(
        values,
        12
    )

    ema26 = ema(
        values,
        26
    )

    macd_line = [
        a - b
        for a, b
        in zip(
            ema12,
            ema26
        )
    ]

    signal_line = ema(
        macd_line,
        9
    )

    if not macd_line:
        return 0.0, 0.0

    return (
        macd_line[-1],
        signal_line[-1]
    )


# ============================================================
# DISCORD BOT REST API
# ============================================================

def send_discord(
    embed,
    return_error=False
):

    if not DISCORD_BOT_TOKEN:
        error = (
            "DISCORD_BOT_TOKEN "
            "is missing or empty."
        )

        print(
            "Discord error:",
            error,
            flush=True
        )

        return (
            (False, error)
            if return_error
            else False
        )

    if not DISCORD_CHANNEL_ID:
        error = (
            "DISCORD_CHANNEL_ID "
            "is missing or empty."
        )

        print(
            "Discord error:",
            error,
            flush=True
        )

        return (
            (False, error)
            if return_error
            else False
        )

    url = (
        "https://discord.com/api/v10/"
        "channels/"
        f"{DISCORD_CHANNEL_ID}"
        "/messages"
    )

    payload = {
        "username": "Cash Gang BTC Radar",
        "embeds": [embed]
    }

    data = json.dumps(
        payload
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization":
                f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type":
                "application/json",
            "User-Agent":
                "PKLA-BTC-Radar/5.0"
        },
        method="POST"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=15
        ) as response:

            status = response.status

            print(
                "Discord Bot API response:",
                status,
                flush=True
            )

            if 200 <= status < 300:

                return (
                    (True, None)
                    if return_error
                    else True
                )

            error = (
                "Discord Bot API returned "
                f"HTTP {status}"
            )

            print(
                "Discord error:",
                error,
                flush=True
            )

            return (
                (False, error)
                if return_error
                else False
            )

    except urllib.error.HTTPError as error:

        body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        detail = (
            f"HTTP {error.code}: {body}"
        )

        print(
            "Discord error:",
            detail,
            flush=True
        )

        return (
            (False, detail)
            if return_error
            else False
        )

    except urllib.error.URLError as error:

        detail = (
            f"URL error: {error.reason}"
        )

        print(
            "Discord error:",
            detail,
            flush=True
        )

        return (
            (False, detail)
            if return_error
            else False
        )

    except Exception as error:

        detail = (
            f"{type(error).__name__}: "
            f"{error}"
        )

        print(
            "Discord error:",
            detail,
            flush=True
        )

        return (
            (False, detail)
            if return_error
            else False
        )


# ============================================================
# BTC ANALYSIS
# ============================================================

def analyze_btc():

    candles = get_btc_candles()

    if len(candles) < 60:
        raise RuntimeError(
            "Not enough Coinbase candles."
        )

    # The newest Coinbase candle can still
    # be forming, so exclude it.
    closed = candles[:-1]

    if len(closed) < 50:
        raise RuntimeError(
            "Not enough closed candles."
        )

    opens = [
        float(c[3])
        for c in closed
    ]

    highs = [
        float(c[2])
        for c in closed
    ]

    lows = [
        float(c[1])
        for c in closed
    ]

    closes = [
        float(c[4])
        for c in closed
    ]

    volumes = [
        float(c[5])
        for c in closed
    ]

    candle_times = [
        int(c[0])
        for c in closed
    ]

    price = closes[-1]

    current_open = opens[-1]
    current_high = highs[-1]
    current_low = lows[-1]
    current_volume = volumes[-1]

    ema9 = ema(
        closes,
        9
    )[-1]

    ema21 = ema(
        closes,
        21
    )[-1]

    ema40 = ema(
        closes,
        40
    )[-1]

    current_rsi = rsi(
        closes,
        14
    )

    macd_value, macd_signal = macd(
        closes
    )

    volume_window = volumes[-21:-1]

    average_volume = (
        sum(volume_window)
        / len(volume_window)
        if volume_window
        else current_volume
    )

    relative_volume = (
        current_volume
        / average_volume
        if average_volume > 0
        else 1.0
    )

    candle_range = max(
        current_high - current_low,
        0.01
    )

    close_position = (
        price - current_low
    ) / candle_range

    bullish_candle = (
        price > current_open
    )

    bearish_candle = (
        price < current_open
    )

    recent_change = (
        price - closes[-4]
    )

    bullish_momentum = (
        recent_change > 0
    )

    bearish_momentum = (
        recent_change < 0
    )

    previous_high = max(
        highs[-21:-1]
    )

    previous_low = min(
        lows[-21:-1]
    )

    breakout_up = (
        price > previous_high
    )

    breakout_down = (
        price < previous_low
    )

    bullish_score = 0
    bearish_score = 0

    reasons = []

    # EMA
    if ema9 > ema21:

        bullish_score += 2

        reasons.append(
            "🟢 EMA9 > EMA21"
        )

    else:

        bearish_score += 2

        reasons.append(
            "🔴 EMA9 < EMA21"
        )

    # EMA40
    if price > ema40:

        bullish_score += 2

        reasons.append(
            "🟢 BTC above EMA40"
        )

    else:

        bearish_score += 2

        reasons.append(
            "🔴 BTC below EMA40"
        )

    # RSI
    if current_rsi >= 55:

        bullish_score += 2

        reasons.append(
            f"🟢 RSI bullish "
            f"({current_rsi:.1f})"
        )

    elif current_rsi <= 45:

        bearish_score += 2

        reasons.append(
            f"🔴 RSI bearish "
            f"({current_rsi:.1f})"
        )

    elif current_rsi > 50:

        bullish_score += 1

        reasons.append(
            f"🟢 RSI slightly bullish "
            f"({current_rsi:.1f})"
        )

    else:

        bearish_score += 1

        reasons.append(
            f"🔴 RSI slightly bearish "
            f"({current_rsi:.1f})"
        )

    # MACD
    if macd_value > macd_signal:

        bullish_score += 2

        reasons.append(
            "🟢 MACD bullish"
        )

    else:

        bearish_score += 2

        reasons.append(
            "🔴 MACD bearish"
        )

    # Volume
    if relative_volume >= 1.15:

        if bullish_candle:

            bullish_score += 2

            reasons.append(
                "🟢 Strong bullish volume "
                f"({relative_volume:.2f}x)"
            )

        elif bearish_candle:

            bearish_score += 2

            reasons.append(
                "🔴 Strong bearish volume "
                f"({relative_volume:.2f}x)"
            )

    elif relative_volume < 0.80:

        reasons.append(
            f"⚪ Low volume "
            f"({relative_volume:.2f}x)"
        )

    else:

        reasons.append(
            f"⚪ Normal volume "
            f"({relative_volume:.2f}x)"
        )

    # Candle
    if (
        bullish_candle
        and close_position >= 0.70
    ):

        bullish_score += 1

        reasons.append(
            "🟢 Strong bullish candle close"
        )

    elif (
        bearish_candle
        and close_position <= 0.30
    ):

        bearish_score += 1

        reasons.append(
            "🔴 Strong bearish candle close"
        )

    # Momentum
    if bullish_momentum:

        bullish_score += 1

        reasons.append(
            "🟢 Short-term momentum UP"
        )

    elif bearish_momentum:

        bearish_score += 1

        reasons.append(
            "🔴 Short-term momentum DOWN"
        )

    # Breakout
    if breakout_up:

        bullish_score += 3

        reasons.append(
            "🚀 Upside range breakout"
        )

    elif breakout_down:

        bearish_score += 3

        reasons.append(
            "📉 Downside range breakout"
        )

    score_gap = abs(
        bullish_score
        - bearish_score
    )

    strongest_score = max(
        bullish_score,
        bearish_score
    )

    # Signal
    if (
        bullish_score >= 8
        and bullish_score > bearish_score
    ):

        signal = "⬆️ BET UP"
        direction = "UP"

    elif (
        bearish_score >= 8
        and bearish_score > bullish_score
    ):

        signal = "⬇️ BET DOWN"
        direction = "DOWN"

    else:

        signal = "⏸️ NO TRADE"
        direction = "NONE"

    # Confidence
    if direction in (
        "UP",
        "DOWN"
    ):

        confidence = (
            50
            + score_gap * 5
            + max(
                0,
                strongest_score - 8
            ) * 3
        )

    else:

        confidence = 50

    confidence = int(
        max(
            50,
            min(
                95,
                confidence
            )
        )
    )

    hold_candles = (
        2
        if confidence >= 80
        else 1
    )

    # Trade levels
    recent_ranges = [
        highs[i] - lows[i]
        for i in range(
            max(
                0,
                len(highs) - 14
            ),
            len(highs)
        )
    ]

    average_range = (
        sum(recent_ranges)
        / len(recent_ranges)
        if recent_ranges
        else candle_range
    )

    if average_range <= 0:
        average_range = candle_range

    if direction == "UP":

        entry = price

        take_profit = (
            price
            + average_range * 1.30
        )

        stop_loss = (
            price
            - average_range * 0.90
        )

    elif direction == "DOWN":

        entry = price

        take_profit = (
            price
            - average_range * 1.30
        )

        stop_loss = (
            price
            + average_range * 0.90
        )

    else:

        entry = price
        take_profit = price
        stop_loss = price

    candle_timestamp = candle_times[-1]

    candle_datetime = datetime.fromtimestamp(
        candle_timestamp,
        tz=timezone.utc
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


# ============================================================
# DISCORD EMBED
# ============================================================

def build_embed(result):

    direction = result["direction"]

    if direction == "UP":

        title = "🟢 BTC UP SIGNAL"
        color = 5763719

    elif direction == "DOWN":

        title = "🔴 BTC DOWN SIGNAL"
        color = 15548997

    else:

        title = "⚪ BTC NO TRADE"
        color = 9807270

    reasons_text = "\n".join(
        result["reasons"]
    )

    return {

        "title": title,

        "description": (
            "**PKLA BTC 15-Minute Market Radar**\n"
            "Automated technical analysis\n"
            "📨 Scheduled every closed 15-minute candle"
        ),

        "color": color,

        "fields": [

            {
                "name": "₿ BTC PRICE",
                "value": (
                    f"**${result['price']:,.2f}**"
                ),
                "inline": False
            },

            {
                "name": "📊 SIGNAL",
                "value": (
                    f"**{result['signal']}**"
                ),
                "inline": True
            },

            {
                "name": "🎯 CONFIDENCE",
                "value": (
                    f"**{result['confidence']}%**"
                ),
                "inline": True
            },

            {
                "name": "🕯 HOLD",
                "value": (
                    f"**{result['hold_candles']} "
                    "candle(s)**"
                ),
                "inline": True
            },

            {
                "name": "📈 SCORE",
                "value": (
                    f"Bullish: "
                    f"**{result['bullish_score']}**\n"
                    f"Bearish: "
                    f"**{result['bearish_score']}**\n"
                    f"Gap: "
                    f"**{result['score_gap']}**"
                ),
                "inline": True
            },

            {
                "name": "📐 INDICATORS",
                "value": (
                    f"RSI: "
                    f"**{result['rsi']:.1f}**\n"
                    f"MACD: "
                    f"**{result['macd']:.2f}**\n"
                    f"Signal: "
                    f"**{result['macd_signal']:.2f}**\n"
                    f"EMA9: "
                    f"**${result['ema9']:,.2f}**\n"
                    f"EMA21: "
                    f"**${result['ema21']:,.2f}**\n"
                    f"EMA40: "
                    f"**${result['ema40']:,.2f}**"
                ),
                "inline": False
            },

            {
                "name": "📍 TRADE LEVELS",
                "value": (
                    f"Entry: "
                    f"**${result['entry']:,.2f}**\n"
                    f"Take Profit: "
                    f"**${result['take_profit']:,.2f}**\n"
                    f"Stop Loss: "
                    f"**${result['stop_loss']:,.2f}**"
                ),
                "inline": False
            },

            {
                "name": "🔬 ANALYSIS",
                "value": reasons_text,
                "inline": False
            },

            {
                "name": "📊 RANGE",
                "value": (
                    f"Previous High: "
                    f"**${result['previous_high']:,.2f}**\n"
                    f"Previous Low: "
                    f"**${result['previous_low']:,.2f}**\n"
                    f"Relative Volume: "
                    f"**{result['relative_volume']:.2f}x**"
                ),
                "inline": False
            },

            {
                "name": "🕐 CANDLE",
                "value": (
                    f"**{result['candle_datetime'].strftime('%Y-%m-%d %H:%M UTC')}**"
                ),
                "inline": False
            }

        ],

        "footer": {
            "text": (
                "PKLA Signal Hub • "
                "BTC • RSI • MACD • EMA • Volume"
            )
        },

        "timestamp": datetime.now(
            timezone.utc
        ).isoformat()
    }


# ============================================================
# SEND SIGNAL
# ============================================================

def send_signal(result):

    # IMPORTANT:
    # Never skip NO TRADE.
    # Every closed 15-minute candle gets sent.
    print(
        "Sending 15-minute result to Discord...",
        flush=True
    )

    embed = build_embed(
        result
    )

    success = send_discord(
        embed
    )

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


# ============================================================
# MAIN RADAR LOOP
# ============================================================

def run_radar():

    print(
        "==========================================",
        flush=True
    )

    print(
        "       PKLA BTC DISCORD RADAR",
        flush=True
    )

    print(
        "==========================================",
        flush=True
    )

    print(
        "Data: Coinbase Exchange",
        flush=True
    )

    print(
        "Market: BTC-USD",
        flush=True
    )

    print(
        "Timeframe: 15M",
        flush=True
    )

    print(
        "TradingView webhook: NOT REQUIRED",
        flush=True
    )

    print(
        "Discord: BOT API",
        flush=True
    )

    print(
        "SEND EVERY 15M: ENABLED",
        flush=True
    )

    print(
        f"Check interval: {CHECK_INTERVAL}s",
        flush=True
    )

    print(
        "==========================================",
        flush=True
    )

    if (
        DISCORD_BOT_TOKEN
        and DISCORD_CHANNEL_ID
    ):

        print(
            "Discord bot: CONFIGURED",
            flush=True
        )

    else:

        print(
            "WARNING: DISCORD_BOT_TOKEN or "
            "DISCORD_CHANNEL_ID is not configured.",
            flush=True
        )

    print(
        "Radar thread started.",
        flush=True
    )

    last_processed_candle = None

    while True:

        try:

            print(
                "Checking Coinbase for new closed candle...",
                flush=True
            )

            candles = get_btc_candles()

            if len(candles) < 3:

                print(
                    "Not enough Coinbase data.",
                    flush=True
                )

                time.sleep(
                    CHECK_INTERVAL
                )

                continue

            # Coinbase returns the newest candle,
            # which may still be forming.
            # The candle before it is the latest
            # CLOSED 15-minute candle.

            latest_closed = candles[-2]

            candle_timestamp = int(
                latest_closed[0]
            )

            candle_datetime = (
                datetime.fromtimestamp(
                    candle_timestamp,
                    tz=timezone.utc
                )
            )

            print(
                "Latest closed candle:",
                candle_datetime.isoformat(),
                flush=True
            )

            # Only process each closed candle once.
            if (
                last_processed_candle
                == candle_timestamp
            ):

                print(
                    "No new closed 15M candle yet.",
                    flush=True
                )

                time.sleep(
                    CHECK_INTERVAL
                )

                continue

            print(
                "New closed 15M candle detected!",
                flush=True
            )

            print(
                "Analyzing BTC...",
                flush=True
            )

            result = analyze_btc()

            print(
                "Price:",
                f"${result['price']:,.2f}",
                flush=True
            )

            print(
                "Signal:",
                result["signal"],
                flush=True
            )

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

            # SEND EVERY CANDLE.
            # NO TRADE IS NOT SKIPPED.
            send_signal(
                result
            )

            last_processed_candle = (
                candle_timestamp
            )

            print(
                "Candle processing complete.",
                flush=True
            )

        except urllib.error.HTTPError as error:

            print(
                "HTTP error:",
                error,
                flush=True
            )

        except urllib.error.URLError as error:

            print(
                "Network error:",
                error,
                flush=True
            )

        except Exception as error:

            print(
                "Radar error:",
                repr(error),
                flush=True
            )

        time.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print(
        "Starting PKLA BTC Discord Radar...",
        flush=True
    )

    threading.Thread(
        target=start_web_server,
        daemon=True
    ).start()

    run_radar()