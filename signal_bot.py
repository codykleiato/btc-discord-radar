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
# Discord delivery uses BOT REST API, NOT a webhook.
#
# Features:
# - BTC 15-minute closed-candle analysis
# - Discord Bot API
# - Discord 429 retry/backoff
# - /test endpoint
# - Sends a result every 15-minute candle
# ============================================================

DISCORD_BOT_TOKEN = os.environ.get(
    "DISCORD_BOT_TOKEN", ""
).strip()

DISCORD_CHANNEL_ID = os.environ.get(
    "DISCORD_CHANNEL_ID", ""
).strip()

SEND_NO_TRADE = os.environ.get(
    "SEND_NO_TRADE", "1"
) == "1"

CHECK_INTERVAL = int(
    os.environ.get("CHECK_INTERVAL", "10")
)

DISCORD_MAX_RETRIES = int(
    os.environ.get("DISCORD_MAX_RETRIES", "5")
)

DISCORD_BASE_BACKOFF = float(
    os.environ.get("DISCORD_BASE_BACKOFF", "2")
)

SYMBOL = "BTC-USD"
GRANULARITY = 900

COINBASE_URL = (
    "https://api.exchange.coinbase.com/products/"
    f"{SYMBOL}/candles?granularity={GRANULARITY}"
)

app = Flask(__name__)


# ============================================================
# WEB ENDPOINTS
# ============================================================

@app.route("/")
def home():
    return {
        "status": "online",
        "service": "btc-discord-radar",
        "discord": "bot_api",
        "timeframe": "15M",
        "send_every_15m": True,
        "test_endpoint": "/test"
    }


@app.route("/test")
def test_discord():
    """
    Immediate Discord connection test.

    Open:
    https://YOUR-RENDER-URL.onrender.com/test
    """

    embed = {
        "title": "🧪 PKLA BTC Radar Test",
        "description": (
            "Discord connection test.\n\n"
            "If you can see this message in Discord, "
            "the Render → PKLA Bot → Discord connection "
            "is working."
        ),
        "color": 5763719,
        "fields": [
            {
                "name": "STATUS",
                "value": "✅ Discord Bot API test",
                "inline": False
            },
            {
                "name": "TIMEFRAME",
                "value": "15-minute radar",
                "inline": True
            }
        ],
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
# HTTP / JSON
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


# ============================================================
# COINBASE
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
            values[i] - values[i - 1]
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

    for i in range(period, len(gains)):

        avg_gain = (
            (
                avg_gain * (period - 1)
            )
            + gains[i]
        ) / period

        avg_loss = (
            (
                avg_loss * (period - 1)
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
# DISCORD BOT REST API
# WITH RETRY / BACKOFF
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
        f"{DISCORD_CHANNEL_ID}/messages"
    )

    payload = {
        "embeds": [embed]
    }

    data = json.dumps(
        payload
    ).encode("utf-8")

    last_error = None

    for attempt in range(
        1,
        DISCORD_MAX_RETRIES + 1
    ):

        print(
            f"Discord send attempt "
            f"{attempt}/{DISCORD_MAX_RETRIES}...",
            flush=True
        )

        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization":
                    f"Bot {DISCORD_BOT_TOKEN}",

                "Content-Type":
                    "application/json",

                "Accept":
                    "application/json",

                "User-Agent":
                    "PKLA-BTC-Radar/5.0"
            },
            method="POST"
        )

        try:

            with urllib.request.urlopen(
                request,
                timeout=20
            ) as response:

                status = response.status

                print(
                    "Discord Bot API response:",
                    status,
                    flush=True
                )

                if 200 <= status < 300:

                    print(
                        "Discord message "
                        "successfully delivered.",
                        flush=True
                    )

                    return (
                        (True, None)
                        if return_error
                        else True
                    )

                last_error = (
                    f"Discord returned "
                    f"HTTP {status}"
                )

        except urllib.error.HTTPError as error:

            body = error.read().decode(
                "utf-8",
                errors="replace"
            )

            last_error = (
                f"HTTP {error.code}: {body}"
            )

            print(
                "Discord error:",
                last_error,
                flush=True
            )

            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if error.code == 429:

                retry_after = None

                try:
                    retry_header = (
                        error.headers.get(
                            "Retry-After"
                        )
                    )

                    if retry_header:
                        retry_after = float(
                            retry_header
                        )

                except Exception:
                    retry_after = None

                # Discord may provide retry_after
                # inside the JSON response.
                if retry_after is None:

                    try:
                        response_json = json.loads(
                            body
                        )

                        retry_after = float(
                            response_json.get(
                                "retry_after",
                                0
                            )
                        )

                    except Exception:
                        retry_after = None

                if retry_after is None:
                    retry_after = (
                        DISCORD_BASE_BACKOFF
                        * (2 ** (attempt - 1))
                    )

                # Safety cap.
                retry_after = max(
                    1.0,
                    min(
                        retry_after,
                        60.0
                    )
                )

                print(
                    "Discord rate limit detected.",
                    flush=True
                )

                print(
                    f"Waiting {retry_after:.1f} "
                    f"seconds before retry...",
                    flush=True
                )

                if attempt < DISCORD_MAX_RETRIES:

                    time.sleep(
                        retry_after
                    )

                    continue

            # ------------------------------------------------
            # OTHER RETRYABLE ERRORS
            # ------------------------------------------------

            if error.code in (
                500,
                502,
                503,
                504
            ):

                backoff = min(
                    DISCORD_BASE_BACKOFF
                    * (2 ** (attempt - 1)),
                    60
                )

                print(
                    f"Retryable Discord error. "
                    f"Waiting {backoff:.1f}s...",
                    flush=True
                )

                if attempt < DISCORD_MAX_RETRIES:

                    time.sleep(
                        backoff
                    )

                    continue

            # ------------------------------------------------
            # AUTH / PERMISSION ERRORS
            # ------------------------------------------------

            if error.code in (
                401,
                403,
                404
            ):

                print(
                    "Discord rejected the request. "
                    "Check the bot token, channel ID, "
                    "bot permissions, and channel access.",
                    flush=True
                )

            break

        except urllib.error.URLError as error:

            last_error = (
                f"URL error: {error.reason}"
            )

            print(
                "Discord error:",
                last_error,
                flush=True
            )

            if attempt < DISCORD_MAX_RETRIES:

                backoff = min(
                    DISCORD_BASE_BACKOFF
                    * (2 ** (attempt - 1)),
                    60
                )

                print(
                    f"Network retry in "
                    f"{backoff:.1f}s...",
                    flush=True
                )

                time.sleep(
                    backoff
                )

                continue

            break

        except Exception as error:

            last_error = (
                f"{type(error).__name__}: "
                f"{error}"
            )

            print(
                "Discord error:",
                last_error,
                flush=True
            )

            if attempt < DISCORD_MAX_RETRIES:

                backoff = min(
                    DISCORD_BASE_BACKOFF
                    * (2 ** (attempt - 1)),
                    60
                )

                time.sleep(
                    backoff
                )

                continue

            break

    print(
        "Discord send failed after "
        f"{DISCORD_MAX_RETRIES} attempts.",
        flush=True
    )

    return (
        (False, last_error)
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

    # Latest candle may still be forming.
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
        bullish_score - bearish_score
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

    # Average range
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

    # Trade levels
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
            "Automated technical analysis"
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
                "PKLA Signal Hub • BTC • RSI • "
                "MACD • EMA • Volume"
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

    # SEND_NO_TRADE defaults to 1 now,
    # so Discord receives every 15-minute result.

    if (
        result["direction"] == "NONE"
        and not SEND_NO_TRADE
    ):

        print(
            "NO TRADE signal - Discord message skipped.",
            flush=True
        )

        return

    print(
        "Sending 15-minute result to Discord...",
        flush=True
    )

    success, error_detail = send_discord(
        build_embed(result),
        return_error=True
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

        print(
            "Discord final error:",
            error_detail,
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
        "Discord retry/backoff: ENABLED",
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

            # Coinbase returns newest first after
            # sorting, so candles[-2] is the latest
            # completed candle and candles[-1] is
            # the currently forming candle.

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
                result[
                    "candle_datetime"
                ].isoformat(),
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