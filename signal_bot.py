import os
import json
import time
import urllib.request
import urllib.error
import threading

from flask import Flask
from datetime import datetime, timezone


# ============================================================
# PKLA BTC DISCORD RADAR
# ============================================================
#
# BTC DATA  -> BINANCE
# ANALYSIS  -> EMA + RSI + MACD + VOLUME + BREAKOUT
# OUTPUT    -> DISCORD WEBHOOK
#
# IMPORTANT:
# EVERY CLOSED 15-MINUTE CANDLE IS SENT TO DISCORD.
# NO TRADE RESULTS ARE ALSO SENT.
#
# ============================================================


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

DISCORD_WEBHOOK = os.environ.get(
    "DISCORD_WEBHOOK",
    ""
).strip()

CHECK_INTERVAL = int(
    os.environ.get(
        "CHECK_INTERVAL",
        "10"
    )
)

SYMBOL = "BTCUSDT"

INTERVAL = "15m"

CANDLE_LIMIT = 200


# ============================================================
# RENDER WEB SERVER
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():

    return {
        "status": "online",
        "service": "btc-discord-radar"
    }


# ============================================================
# DISCORD TEST ENDPOINT
# ============================================================

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

    success = send_discord(embed)

    if success:

        return {
            "status": "success",
            "message": "Test message sent to Discord"
        }

    return {
        "status": "error",
        "message": "Discord test failed"
    }, 500


# ============================================================
# WEB SERVER
# ============================================================

def start_web_server():

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )


# ============================================================
# BINANCE
# ============================================================

BINANCE_URL = (
    "https://api.binance.com/api/v3/klines"
    f"?symbol={SYMBOL}"
    f"&interval={INTERVAL}"
    f"&limit={CANDLE_LIMIT}"
)


# ============================================================
# HTTP JSON
# ============================================================

def get_json(url):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PKLA-BTC-Radar/3.0"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=15
    ) as response:

        return json.loads(
            response.read().decode("utf-8")
        )


# ============================================================
# EMA
# ============================================================

def ema(values, period):

    if not values:
        return []

    if len(values) < period:

        return [values[0]] * len(values)

    multiplier = 2.0 / (
        period + 1.0
    )

    result = [
        values[0]
    ]

    for price in values[1:]:

        value = (
            (price - result[-1])
            * multiplier
            + result[-1]
        )

        result.append(value)

    return result


# ============================================================
# RSI
# ============================================================

def rsi(values, period=14):

    if len(values) < period + 1:

        return 50.0

    gains = []
    losses = []

    for i in range(
        1,
        len(values)
    ):

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

        if avg_gain == 0:
            return 50.0

        return 100.0

    relative_strength = (
        avg_gain
        / avg_loss
    )

    return 100.0 - (
        100.0
        / (1.0 + relative_strength)
    )


# ============================================================
# MACD
# ============================================================

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
        for a, b in zip(
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
# DISCORD SEND
# ============================================================

def send_discord(embed):

    if not DISCORD_WEBHOOK:

        print(
            "DISCORD SEND FAILED: "
            "DISCORD_WEBHOOK is missing."
        )

        return False

    payload = {
        "username": "Cash Gang BTC Radar",
        "embeds": [
            embed
        ]
    }

    data = json.dumps(
        payload
    ).encode("utf-8")

    request = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=data,
        headers={
            "Content-Type":
                "application/json",

            "User-Agent":
                "PKLA-BTC-Radar/3.0"
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
                "Discord HTTP response:",
                status
            )

            if 200 <= status < 300:

                print(
                    "DISCORD SEND SUCCESS"
                )

                return True

            print(
                "DISCORD SEND FAILED: "
                f"HTTP {status}"
            )

            return False

    except urllib.error.HTTPError as error:

        print(
            "DISCORD SEND FAILED:"
        )

        print(
            "HTTP status:",
            error.code
        )

        try:

            body = error.read().decode(
                "utf-8",
                errors="replace"
            )

            print(
                "Discord response:",
                body
            )

        except Exception:
            pass

        return False

    except Exception as error:

        print(
            "DISCORD SEND FAILED:",
            repr(error)
        )

        return False


# ============================================================
# GET BTC CANDLES
# ============================================================

def get_btc_candles():

    print(
        "Requesting BTC candles from Binance..."
    )

    candles = get_json(
        BINANCE_URL
    )

    if not candles:

        raise RuntimeError(
            "Binance returned no candles."
        )

    print(
        "Binance returned",
        len(candles),
        "candles."
    )

    return candles


# ============================================================
# ANALYZE BTC
# ============================================================

def analyze_btc():

    candles = get_btc_candles()

    # Binance's final candle is currently open.
    # Everything before it is closed.
    closed = candles[:-1]

    if len(closed) < 50:

        raise RuntimeError(
            "Not enough closed candles."
        )

    opens = [
        float(candle[1])
        for candle in closed
    ]

    highs = [
        float(candle[2])
        for candle in closed
    ]

    lows = [
        float(candle[3])
        for candle in closed
    ]

    closes = [
        float(candle[4])
        for candle in closed
    ]

    volumes = [
        float(candle[5])
        for candle in closed
    ]

    candle_times = [
        int(candle[0])
        for candle in closed
    ]

    price = closes[-1]

    previous_price = closes[-2]

    current_open = opens[-1]

    current_high = highs[-1]

    current_low = lows[-1]

    current_volume = volumes[-1]

    ema9_values = ema(
        closes,
        9
    )

    ema21_values = ema(
        closes,
        21
    )

    ema40_values = ema(
        closes,
        40
    )

    ema9 = ema9_values[-1]

    ema21 = ema21_values[-1]

    ema40 = ema40_values[-1]

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

    candle_body = abs(
        price - current_open
    )

    close_position = (
        (price - current_low)
        / candle_range
    )

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

    previous_highs = highs[-21:-1]

    previous_lows = lows[-21:-1]

    previous_high = max(
        previous_highs
    )

    previous_low = min(
        previous_lows
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

    # EMA TREND
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
            f"🟢 RSI bullish ({current_rsi:.1f})"
        )

    elif current_rsi <= 45:

        bearish_score += 2

        reasons.append(
            f"🔴 RSI bearish ({current_rsi:.1f})"
        )

    elif current_rsi > 50:

        bullish_score += 1

        reasons.append(
            f"🟢 RSI slightly bullish ({current_rsi:.1f})"
        )

    else:

        bearish_score += 1

        reasons.append(
            f"🔴 RSI slightly bearish ({current_rsi:.1f})"
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

    # VOLUME
    if relative_volume >= 1.15:

        if bullish_candle:

            bullish_score += 2

            reasons.append(
                f"🟢 Strong bullish volume "
                f"({relative_volume:.2f}x)"
            )

        elif bearish_candle:

            bearish_score += 2

            reasons.append(
                f"🔴 Strong bearish volume "
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

    # CANDLE CLOSE
    if candle_range > 0:

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

    # MOMENTUM
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

    # BREAKOUT
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

    # FINAL SIGNAL
    score_gap = abs(
        bullish_score
        - bearish_score
    )

    strongest_score = max(
        bullish_score,
        bearish_score
    )

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

    # CONFIDENCE
    if direction in (
        "UP",
        "DOWN"
    ):

        confidence = (
            50
            + (score_gap * 5)
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

    # HOLD
    if confidence >= 80:

        hold_candles = 2

    else:

        hold_candles = 1

    # RANGE
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

    # TRADE LEVELS
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
        candle_timestamp / 1000,
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

        "breakout_up": breakout_up,

        "breakout_down": breakout_down,

        "entry": entry,

        "take_profit": take_profit,

        "stop_loss": stop_loss,

        "hold_candles": hold_candles,

        "reasons": reasons,

        "candle_timestamp": candle_timestamp,

        "candle_datetime": candle_datetime
    }


# ============================================================
# BUILD DISCORD MESSAGE
# ============================================================

def build_embed(result):

    signal = result["signal"]

    direction = result["direction"]

    confidence = result["confidence"]

    price = result["price"]

    if direction == "UP":

        title = "🟢 BTC UP SIGNAL"

    elif direction == "DOWN":

        title = "🔴 BTC DOWN SIGNAL"

    else:

        title = "⚪ BTC NO TRADE"

    reasons_text = "\n".join(
        result["reasons"]
    )

    if not reasons_text:

        reasons_text = (
            "No additional conditions."
        )

    candle_time = (
        result["candle_datetime"]
        .strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    )

    embed = {

        "title": title,

        "description": (
            "**PKLA BTC 15-Minute "
            "Market Radar**\n"
            "Automated technical analysis\n\n"
            f"🕐 Closed candle: **{candle_time}**"
        ),

        "color": (
            5763719
            if direction == "UP"
            else
            15548997
            if direction == "DOWN"
            else
            9807270
        ),

        "fields": [

            {
                "name": "₿ BTC PRICE",

                "value": (
                    f"**${price:,.2f}**"
                ),

                "inline": False
            },

            {
                "name": "📊 SIGNAL",

                "value": (
                    f"**{signal}**"
                ),

                "inline": True
            },

            {
                "name": "🎯 CONFIDENCE",

                "value": (
                    f"**{confidence}%**"
                ),

                "inline": True
            },

            {
                "name": "🕯 HOLD",

                "value": (
                    f"**{result['hold_candles']} "
                    f"candle(s)**"
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

    return embed


# ============================================================
# SEND CANDLE ANALYSIS
# ============================================================
#
# IMPORTANT:
# There is intentionally NO NO-TRADE skip here.
#
# Every closed candle gets sent to Discord.
#
# ============================================================

def send_signal(result):

    candle_time = (
        result["candle_datetime"]
        .strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    )

    print(
        "--------------------------------------------------"
    )

    print(
        "Sending candle analysis to Discord..."
    )

    print(
        "Candle:",
        candle_time
    )

    print(
        "Result:",
        result["signal"]
    )

    embed = build_embed(
        result
    )

    success = send_discord(
        embed
    )

    if success:

        print(
            "DISCORD SEND SUCCESS:"
        )

        print(
            f"15M candle {candle_time} "
            "was sent to Discord."
        )

    else:

        print(
            "DISCORD SEND FAILED:"
        )

        print(
            f"15M candle {candle_time} "
            "was NOT sent to Discord."
        )

    print(
        "--------------------------------------------------"
    )

    return success


# ============================================================
# IMMEDIATE TEST ANALYSIS
# ============================================================

def run_test_analysis():

    print(
        "Running immediate BTC analysis test..."
    )

    result = analyze_btc()

    print(
        "Test candle:",
        result["candle_datetime"]
        .strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    )

    print(
        "Test signal:",
        result["signal"]
    )

    success = send_signal(
        result
    )

    return (
        result,
        success
    )


# ============================================================
# OPTIONAL ANALYSIS TEST ENDPOINT
# ============================================================

@app.route("/test-analysis")
def test_analysis():

    try:

        result, success = (
            run_test_analysis()
        )

        if success:

            return {

                "status": "success",

                "message": (
                    "BTC analysis generated "
                    "and sent to Discord."
                ),

                "signal": result["signal"],

                "price": result["price"],

                "confidence":
                    result["confidence"],

                "candle":
                    result["candle_datetime"]
                    .isoformat()
            }

        return {

            "status": "error",

            "message": (
                "BTC analysis generated, "
                "but Discord send failed."
            ),

            "signal": result["signal"],

            "price": result["price"]

        }, 500

    except Exception as error:

        print(
            "Test analysis error:",
            repr(error)
        )

        return {

            "status": "error",

            "message": str(error)

        }, 500


# ============================================================
# MAIN RADAR LOOP
# ============================================================

def run_radar():

    print(
        "=========================================="
    )

    print(
        "       PKLA BTC DISCORD RADAR"
    )

    print(
        "=========================================="
    )

    print(
        "Data: Binance"
    )

    print(
        "Market: BTCUSDT"
    )

    print(
        "Timeframe: 15M"
    )

    print(
        "TradingView webhook: NOT REQUIRED"
    )

    print(
        "Discord: ENABLED"
    )

    print(
        "Every closed candle: SEND"
    )

    print(
        "NO TRADE candles: SEND"
    )

    print(
        "=========================================="
    )

    if not DISCORD_WEBHOOK:

        print(
            "WARNING:"
        )

        print(
            "DISCORD_WEBHOOK environment variable "
            "has not been configured."
        )

        print(
            "The bot can analyze BTC but "
            "cannot send Discord messages."
        )

    else:

        print(
            "Discord webhook: CONFIGURED"
        )

    last_processed_candle = None

    while True:

        try:

            candles = get_btc_candles()

            if len(candles) < 3:

                print(
                    "Not enough Binance data."
                )

                time.sleep(
                    CHECK_INTERVAL
                )

                continue

            # Binance's final candle is open.
            # The candle immediately before it is closed.

            latest_closed = candles[-2]

            candle_timestamp = int(
                latest_closed[0]
            )

            candle_datetime = (
                datetime.fromtimestamp(
                    candle_timestamp / 1000,
                    tz=timezone.utc
                )
            )

            if (
                last_processed_candle
                == candle_timestamp
            ):

                time.sleep(
                    CHECK_INTERVAL
                )

                continue

            print(
                "\n=========================================="
            )

            print(
                "NEW CLOSED 15M CANDLE DETECTED"
            )

            print(
                "Candle:",
                candle_datetime.strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            )

            print(
                "Analyzing BTC..."
            )

            result = analyze_btc()

            print(
                "Price:",
                f"${result['price']:,.2f}"
            )

            print(
                "Signal:",
                result["signal"]
            )

            print(
                "Confidence:",
                f"{result['confidence']}%"
            )

            print(
                "Bullish score:",
                result["bullish_score"]
            )

            print(
                "Bearish score:",
                result["bearish_score"]
            )

            # ====================================================
            # ALWAYS SEND.
            #
            # NO TRADE IS NOT SKIPPED.
            # ====================================================

            send_success = send_signal(
                result
            )

            # ====================================================
            # IMPORTANT:
            #
            # Only mark this candle processed after the Discord
            # send succeeds.
            #
            # If Discord fails, the bot will retry this candle
            # on the next loop instead of losing it.
            # ====================================================

            if send_success:

                last_processed_candle = (
                    candle_timestamp
                )

                print(
                    "Candle processing complete."
                )

            else:

                print(
                    "Discord send failed."
                )

                print(
                    "Candle NOT marked processed."
                )

                print(
                    "Will retry on next check."
                )

        except urllib.error.HTTPError as error:

            print(
                "HTTP error:",
                error
            )

        except urllib.error.URLError as error:

            print(
                "Network error:",
                error
            )

        except Exception as error:

            print(
                "Radar error:",
                repr(error)
            )

        time.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print(
        "Starting PKLA BTC Discord Radar..."
    )

    # Start Flask so Render detects an open port.

    threading.Thread(
        target=start_web_server,
        daemon=True
    ).start()

    print(
        "Radar thread started."
    )

    # Start BTC radar.

    run_radar()