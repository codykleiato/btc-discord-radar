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
# BTC DATA  -> COINBASE EXCHANGE
# TIMEFRAME  -> 15 MINUTES
# ANALYSIS   -> EMA + RSI + MACD + VOLUME + BREAKOUT
# OUTPUT     -> DISCORD WEBHOOK
#
# TRADINGVIEW WEBHOOK NOT REQUIRED
# ============================================================


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

DISCORD_WEBHOOK = os.environ.get(
    "DISCORD_WEBHOOK",
    ""
).strip()

SEND_NO_TRADE = (
    os.environ.get(
        "SEND_NO_TRADE",
        "0"
    ) == "1"
)

CHECK_INTERVAL = int(
    os.environ.get(
        "CHECK_INTERVAL",
        "10"
    )
)

SYMBOL = "BTC-USD"

GRANULARITY = 900

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
        os.environ.get(
            "PORT",
            "10000"
        )
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
# COINBASE EXCHANGE API
# ============================================================

COINBASE_URL = (
    "https://api.exchange.coinbase.com/products/"
    f"{SYMBOL}/candles"
    f"?granularity={GRANULARITY}"
)


# ============================================================
# HTTP JSON
# ============================================================

def get_json(url):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PKLA-BTC-Radar/3.0",
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


# ============================================================
# GET BTC CANDLES FROM COINBASE
# ============================================================

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

    # Coinbase candle format:
    #
    # [
    #   time,
    #   low,
    #   high,
    #   open,
    #   close,
    #   volume
    # ]
    #
    # Coinbase normally returns newest first.
    # Reverse it so candles are oldest -> newest.

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
        / (
            1.0
            + relative_strength
        )
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
# DISCORD
# ============================================================

def send_discord(
    embed,
    return_error=False
):

    if not DISCORD_WEBHOOK:

        error = (
            "DISCORD_WEBHOOK is missing or empty."
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

    payload = {
        "username": "Cash Gang BTC Radar",
        "embeds": [embed]
    }

    data = json.dumps(
        payload
    ).encode("utf-8")

    request = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "PKLA-BTC-Radar/3.0"
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
                "Discord response:",
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
                f"Discord returned HTTP {status}"
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
            f"{type(error).__name__}: {error}"
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
# ANALYZE BTC
# ============================================================

def analyze_btc():

    candles = get_btc_candles()

    if len(candles) < 60:

        raise RuntimeError(
            "Not enough Coinbase candles."
        )

    # Remove the currently forming candle.
    #
    # Coinbase returns the most recent candle,
    # which may still be forming.

    closed = candles[:-1]

    if len(closed) < 50:

        raise RuntimeError(
            "Not enough closed candles."
        )

    opens = [
        float(candle[3])
        for candle in closed
    ]

    highs = [
        float(candle[2])
        for candle in closed
    ]

    lows = [
        float(candle[1])
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
                f"🟢 Strong bullish volume ({relative_volume:.2f}x)"
            )

        elif bearish_candle:

            bearish_score += 2

            reasons.append(
                f"🔴 Strong bearish volume ({relative_volume:.2f}x)"
            )

    elif relative_volume < 0.80:

        reasons.append(
            f"⚪ Low volume ({relative_volume:.2f}x)"
        )

    else:

        reasons.append(
            f"⚪ Normal volume ({relative_volume:.2f}x)"
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

    score_gap = abs(
        bullish_score
        - bearish_score
    )

    strongest_score = max(
        bullish_score,
        bearish_score
    )

    # SIGNAL
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

    direction = result["direction"]

    price = result["price"]

    confidence = result["confidence"]

    signal = result["signal"]

    if direction == "UP":

        title = "🟢 BTC UP SIGNAL"

    elif direction == "DOWN":

        title = "🔴 BTC DOWN SIGNAL"

    else:

        title = "⚪ BTC NO TRADE"

    reasons_text = "\n".join(
        result["reasons"]
    )

    embed = {

        "title": title,

        "description": (
            "**PKLA BTC 15-Minute Market Radar**\n"
            "Automated technical analysis"
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
                "value": f"**${price:,.2f}**",
                "inline": False
            },

            {
                "name": "📊 SIGNAL",
                "value": f"**{signal}**",
                "inline": True
            },

            {
                "name": "🎯 CONFIDENCE",
                "value": f"**{confidence}%**",
                "inline": True
            },

            {
                "name": "🕯 HOLD",
                "value": (
                    f"**{result['hold_candles']} candle(s)**"
                ),
                "inline": True
            },

            {
                "name": "📈 SCORE",
                "value": (
                    f"Bullish: **{result['bullish_score']}**\n"
                    f"Bearish: **{result['bearish_score']}**\n"
                    f"Gap: **{result['score_gap']}**"
                ),
                "inline": True
            },

            {
                "name": "📐 INDICATORS",
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
                "name": "📍 TRADE LEVELS",
                "value": (
                    f"Entry: **${result['entry']:,.2f}**\n"
                    f"Take Profit: **${result['take_profit']:,.2f}**\n"
                    f"Stop Loss: **${result['stop_loss']:,.2f}**"
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
                    f"Previous High: **${result['previous_high']:,.2f}**\n"
                    f"Previous Low: **${result['previous_low']:,.2f}**\n"
                    f"Relative Volume: **{result['relative_volume']:.2f}x**"
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
# SEND SIGNAL
# ============================================================

def send_signal(result):

    if (
        result["direction"] == "NONE"
        and not SEND_NO_TRADE
    ):

        print(
            "NO TRADE signal - Discord message skipped.",
            flush=True
        )

        return

    embed = build_embed(
        result
    )

    print(
        "Sending signal to Discord...",
        flush=True
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
        "Discord: ENABLED",
        flush=True
    )

    print(
        "Check interval:",
        f"{CHECK_INTERVAL}s",
        flush=True
    )

    print(
        "==========================================",
        flush=True
    )

    if DISCORD_WEBHOOK:

        print(
            "Discord webhook: CONFIGURED",
            flush=True
        )

    else:

        print(
            "WARNING: DISCORD_WEBHOOK is not configured.",
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

            # Most recent candle may still be forming.
            # Second-to-last candle is the latest closed candle.

            latest_closed = candles[-2]

            candle_timestamp = int(
                latest_closed[0]
            )

            candle_datetime = datetime.fromtimestamp(
                candle_timestamp,
                tz=timezone.utc
            )

            print(
                "Latest closed candle:",
                candle_datetime.isoformat(),
                flush=True
            )

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
                "",
                flush=True
            )

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