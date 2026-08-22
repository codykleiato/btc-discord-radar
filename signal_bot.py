import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, jsonify


# =============================================================================
# BTC 15-MINUTE DISCORD RADAR
#
# EXACT CALLOUT STYLE:
#   BTC UP SIGNAL / BTC DOWN SIGNAL
#   BTC PRICE
#   SIGNAL
#   CONFIDENCE
#   HOLD
#   SCORE
#   INDICATORS
#   TRADE LEVELS
#   15-MINUTE TARGET
#   ANALYSIS
#
# DATA:
#   Coinbase BTC-USD
#
# TIMING:
#   Sends once as soon as a new 15-minute candle opens.
#
# RELIABILITY:
#   Uses the CLOCK to detect new 15-minute candles.
#   Does not depend on Coinbase publishing the new candle immediately.
#   Retries Coinbase and Discord requests automatically.
#   Saves state so duplicate signals are avoided.
#
# RENDER:
#   Includes HTTP health server.
# =============================================================================


# =============================================================================
# RENDER HEALTH SERVER
# =============================================================================

app = Flask(__name__)


@app.route("/")
def home():
    return jsonify(
        {
            "status": "online",
            "service": "BTC 15-Minute Market Radar",
            "timeframe": "15m",
            "data_source": "Coinbase BTC-USD",
        }
    )


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "healthy",
            "service": "BTC 15-Minute Market Radar",
        }
    )


def run_web_server():
    port = int(os.getenv("PORT", "10000"))

    logger.info(
        "Starting Render health server on port %s...",
        port,
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )


# =============================================================================
# SETTINGS
# =============================================================================

DISCORD_WEBHOOK_URL = os.getenv(
    "DISCORD_WEBHOOK_URL",
    "",
).strip()

POLL_SECONDS = int(
    os.getenv(
        "POLL_SECONDS",
        "5",
    )
)

TIMEFRAME_SECONDS = 15 * 60

COINBASE_URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/candles"
)

STATE_FILE = Path(
    os.getenv(
        "STATE_FILE",
        "btc_radar_state.json",
    )
)

LOG_FILE = Path(
    os.getenv(
        "LOG_FILE",
        "btc_radar.log",
    )
)

RUNNING = True


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            LOG_FILE,
            encoding="utf-8",
        ),
    ],
)

logger = logging.getLogger(
    "btc-radar"
)


# =============================================================================
# HTTP SESSION
# =============================================================================

def build_session() -> requests.Session:

    retries = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.0,
        status_forcelist=(
            408,
            429,
            500,
            502,
            503,
            504,
        ),
        allowed_methods=frozenset(
            ["GET", "POST"]
        ),
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    session = requests.Session()

    session.mount(
        "https://",
        HTTPAdapter(
            max_retries=retries
        ),
    )

    session.headers.update(
        {
            "User-Agent": "btc-15m-radar/1.0",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        }
    )

    return session


SESSION = build_session()


# =============================================================================
# STATE
# =============================================================================

def default_state():

    return {
        "service": "btc-15m-radar",
        "active_window": None,
        "last_sent_window": None,
        "status": "running",
        "last_error": None,
        "last_sent_at": None,
    }


def load_state():

    if not STATE_FILE.exists():
        return default_state()

    try:

        state = default_state()

        state.update(
            json.loads(
                STATE_FILE.read_text(
                    encoding="utf-8"
                )
            )
        )

        return state

    except Exception as error:

        logger.warning(
            "Could not load state: %s",
            error,
        )

        return default_state()


def save_state(state):

    try:

        STATE_FILE.write_text(
            json.dumps(
                state,
                indent=2,
            ),
            encoding="utf-8",
        )

    except OSError as error:

        logger.error(
            "Could not save state: %s",
            error,
        )


# =============================================================================
# TIME HELPERS
# =============================================================================

def utc_now():

    return datetime.now(
        timezone.utc
    )


def iso_now():

    return utc_now().isoformat()


def floor_15m_timestamp(now):

    timestamp = int(
        now.timestamp()
    )

    return timestamp - (
        timestamp % TIMEFRAME_SECONDS
    )


def candle_start_datetime(timestamp):

    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc,
    )


def format_window(timestamp):

    return candle_start_datetime(
        timestamp
    ).strftime(
        "%H:%M UTC"
    )


def money(value):

    if value is None:
        return "N/A"

    return f"${value:,.2f}"


def signed_money(value):

    return f"{value:+,.2f}"


# =============================================================================
# COINBASE DATA
# =============================================================================

def get_coinbase_candles():

    try:

        response = SESSION.get(
            COINBASE_URL,
            params={
                "granularity": 900
            },
            timeout=30,
        )

        response.raise_for_status()

        rows = response.json()

        if not isinstance(
            rows,
            list
        ):

            raise ValueError(
                "Coinbase returned invalid candle data."
            )

        candles = []

        for row in rows:

            if len(row) < 5:
                continue

            timestamp = int(
                row[0]
            )

            low = float(
                row[1]
            )

            high = float(
                row[2]
            )

            open_price = float(
                row[3]
            )

            close = float(
                row[4]
            )

            candles.append(
                {
                    "timestamp": timestamp,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                }
            )

        candles.sort(
            key=lambda x:
            x["timestamp"]
        )

        if not candles:

            raise ValueError(
                "No Coinbase candles returned."
            )

        return candles

    except Exception as error:

        logger.error(
            "Coinbase request failed: %s",
            error,
        )

        return []


# =============================================================================
# EMA
# =============================================================================

def calculate_ema(
    values,
    period,
):

    if not values:
        return []

    multiplier = (
        2 / (period + 1)
    )

    ema_values = []

    ema = values[0]

    ema_values.append(
        ema
    )

    for value in values[1:]:

        ema = (
            (value - ema)
            * multiplier
            + ema
        )

        ema_values.append(
            ema
        )

    return ema_values


# =============================================================================
# RSI
# =============================================================================

def calculate_rsi(
    closes,
    period=14,
):

    if len(closes) < period + 1:

        return 50.0

    gains = []
    losses = []

    for i in range(1, len(closes)):

        change = (
            closes[i]
            - closes[i - 1]
        )

        if change > 0:

            gains.append(change)
            losses.append(0)

        else:

            gains.append(0)
            losses.append(
                abs(change)
            )

    avg_gain = sum(
        gains[:period]
    ) / period

    avg_loss = sum(
        losses[:period]
    ) / period

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

        return 100.0

    rs = (
        avg_gain
        / avg_loss
    )

    rsi = (
        100
        - (
            100
            / (1 + rs)
        )
    )

    return rsi


# =============================================================================
# MACD
# =============================================================================

def calculate_macd(
    closes,
):

    ema12 = calculate_ema(
        closes,
        12,
    )

    ema26 = calculate_ema(
        closes,
        26,
    )

    macd_values = []

    for i in range(
        len(closes)
    ):

        macd_values.append(
            ema12[i]
            - ema26[i]
        )

    signal_values = calculate_ema(
        macd_values,
        9,
    )

    macd = macd_values[-1]
    signal = signal_values[-1]

    return (
        macd,
        signal,
    )


# =============================================================================
# TECHNICAL ANALYSIS
# =============================================================================

def analyze_market(candles):

    closed = candles

    if len(closed) < 50:

        raise ValueError(
            "Not enough closed candles for analysis."
        )

    closes = [
        candle["close"]
        for candle in closed
    ]

    current_price = closes[-1]

    ema9_values = calculate_ema(
        closes,
        9,
    )

    ema21_values = calculate_ema(
        closes,
        21,
    )

    ema40_values = calculate_ema(
        closes,
        40,
    )

    ema9 = ema9_values[-1]
    ema21 = ema21_values[-1]
    ema40 = ema40_values[-1]

    rsi = calculate_rsi(
        closes,
        14,
    )

    macd, macd_signal = (
        calculate_macd(
            closes
        )
    )

    bullish = 0
    bearish = 0

    analysis = []

    # -------------------------------------------------------------------------
    # EMA 9 / EMA 21
    # -------------------------------------------------------------------------

    if ema9 > ema21:

        bullish += 1

        analysis.append(
            "🟢 EMA9 > EMA21"
        )

    else:

        bearish += 1

        analysis.append(
            "🔴 EMA9 < EMA21"
        )

    # -------------------------------------------------------------------------
    # PRICE VS EMA40
    # -------------------------------------------------------------------------

    if current_price > ema40:

        bullish += 1

        analysis.append(
            "🟢 BTC above EMA40"
        )

    else:

        bearish += 1

        analysis.append(
            "🔴 BTC below EMA40"
        )

    # -------------------------------------------------------------------------
    # RSI
    # -------------------------------------------------------------------------

    if rsi >= 50:

        bullish += 1

        analysis.append(
            f"🟢 RSI bullish ({rsi:.1f})"
        )

    else:

        bearish += 1

        analysis.append(
            f"🔴 RSI bearish ({rsi:.1f})"
        )

    # -------------------------------------------------------------------------
    # MACD
    # -------------------------------------------------------------------------

    if macd > macd_signal:

        bullish += 1

        analysis.append(
            "🟢 MACD bullish"
        )

    else:

        bearish += 1

        analysis.append(
            "🔴 MACD bearish"
        )

    # -------------------------------------------------------------------------
    # PRICE VS TARGET
    # -------------------------------------------------------------------------

    ema_mid = (
        ema9 + ema21
    ) / 2

    distance_to_ema = (
        ema_mid
        - current_price
    )

    if bullish >= bearish:

        target = (
            current_price
            + (
                abs(
                    distance_to_ema
                )
                * 1.5
            )
        )

        if target <= current_price:

            target = (
                current_price
                + (
                    current_price
                    * 0.00045
                )
            )

        bullish += 1

        analysis.append(
            "🟢 BTC above 15m target"
            if current_price > target
            else
            "🟢 BTC bullish target"
        )

    else:

        target = (
            current_price
            - (
                abs(
                    distance_to_ema
                )
                * 1.5
            )
        )

        if target >= current_price:

            target = (
                current_price
                - (
                    current_price
                    * 0.00045
                )
            )

        bearish += 1

        analysis.append(
            "🔴 BTC below 15m target"
            if current_price < target
            else
            "🔴 BTC bearish target"
        )

    # -------------------------------------------------------------------------
    # SIGNAL
    # -------------------------------------------------------------------------

    if bullish > bearish:

        direction = "UP"

    elif bearish > bullish:

        direction = "DOWN"

    else:

        direction = "HOLD"

    # -------------------------------------------------------------------------
    # CONFIDENCE
    # -------------------------------------------------------------------------

    total = (
        bullish
        + bearish
    )

    if total:

        confidence = round(
            (
                max(
                    bullish,
                    bearish,
                )
                / total
            )
            * 100
        )

    else:

        confidence = 50

    confidence = max(
        50,
        min(
            confidence,
            100,
        ),
    )

    # -------------------------------------------------------------------------
    # SCORE GAP
    # -------------------------------------------------------------------------

    gap = (
        bullish
        - bearish
    )

    # -------------------------------------------------------------------------
    # TRADE LEVELS
    # -------------------------------------------------------------------------

    entry = current_price

    if direction == "UP":

        take_profit = (
            entry
            + (
                entry
                * 0.00030
            )
        )

        stop_loss = (
            entry
            - (
                entry
                * 0.00020
            )
        )

    elif direction == "DOWN":

        take_profit = (
            entry
            - (
                entry
                * 0.00030
            )
        )

        stop_loss = (
            entry
            + (
                entry
                * 0.00020
            )
        )

    else:

        take_profit = (
            entry
            + (
                entry
                * 0.00020
            )
        )

        stop_loss = (
            entry
            - (
                entry
                * 0.00020
            )
        )

    # -------------------------------------------------------------------------
    # TARGET
    # -------------------------------------------------------------------------

    if direction == "UP":

        target = max(
            target,
            entry
            + (
                entry
                * 0.00015
            ),
        )

    elif direction == "DOWN":

        target = min(
            target,
            entry
            - (
                entry
                * 0.00015
            ),
        )

    target_difference = (
        target
        - entry
    )

    # -------------------------------------------------------------------------
    # WINDOW
    # -------------------------------------------------------------------------

    next_candle_window = (
        datetime.fromtimestamp(
            candles[-1]["timestamp"],
            tz=timezone.utc,
        )
    )

    return {
        "direction": direction,
        "price": current_price,
        "confidence": confidence,
        "bullish": bullish,
        "bearish": bearish,
        "gap": gap,
        "rsi": rsi,
        "macd": macd,
        "macd_signal": macd_signal,
        "ema9": ema9,
        "ema21": ema21,
        "ema40": ema40,
        "entry": entry,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "target": target,
        "target_difference": target_difference,
        "window": next_candle_window.strftime(
            "%H:%M UTC"
        ),
        "analysis": analysis,
    }


# =============================================================================
# DISCORD COLORS
# =============================================================================

def direction_color(
    direction
):

    if direction == "UP":

        return (
            0x2ECC71,
            "🟢",
            "BTC UP SIGNAL",
            "BET UP",
        )

    if direction == "DOWN":

        return (
            0xE74C3C,
            "🔴",
            "BTC DOWN SIGNAL",
            "BET DOWN",
        )

    return (
        0x95A5A6,
        "⚪",
        "BTC HOLD SIGNAL",
        "HOLD",
    )


# =============================================================================
# DISCORD EMBED
# =============================================================================

def build_embed(
    result,
):

    (
        color,
        signal_emoji,
        signal_title,
        bet_text,
    ) = direction_color(
        result["direction"]
    )

    hold_text = (
        "2 candle(s) / up to 30 minutes"
    )

    analysis_text = "\n".join(
        result["analysis"]
    )

    description = (
        f"**BTC 15-Minute Market Radar**\n"
        f"Coinbase BTC-USD technical analysis\n\n"

        f"₿ **BTC PRICE**\n"
        f"**{money(result['price'])}**\n\n"

        f"📊 **SIGNAL**\n"
        f"**{bet_text}**\n\n"

        f"🎯 **CONFIDENCE**\n"
        f"**{result['confidence']}%**\n\n"

        f"🕯️ **HOLD**\n"
        f"**{hold_text}**\n\n"

        f"📈 **SCORE**\n"
        f"Bullish: **{result['bullish']}**\n"
        f"Bearish: **{result['bearish']}**\n"
        f"Gap: **{result['gap']:+d}**\n\n"

        f"📐 **INDICATORS**\n"
        f"RSI: **{result['rsi']:.1f}**\n"
        f"MACD: **{result['macd']:.2f}**\n"
        f"Signal: **{result['macd_signal']:.2f}**\n"
        f"EMA9: **{money(result['ema9'])}**\n"
        f"EMA21: **{money(result['ema21'])}**\n"
        f"EMA40: **{money(result['ema40'])}**\n\n"

        f"📍 **TRADE LEVELS**\n"
        f"Entry: **{money(result['entry'])}**\n"
        f"Take Profit: **{money(result['take_profit'])}**\n"
        f"Stop Loss: **{money(result['stop_loss'])}**\n\n"

        f"🎯 **15-MINUTE TARGET**\n"
        f"Target: **{money(result['target'])}**\n"
        f"Difference: **"
        f"{signed_money(result['target_difference'])}"
        f"**\n"
        f"Window: **{result['window']}**\n\n"

        f"🔬 **ANALYSIS**\n"
        f"{analysis_text}"
    )

    return {
        "title": (
            f"{signal_emoji} {signal_title}"
        ),

        "description": description,

        "color": color,

        "footer": {
            "text": (
                "BTC 15-Minute Market Radar"
            )
        },

        "timestamp": iso_now(),
    }


# =============================================================================
# SEND DISCORD
# =============================================================================

def send_discord(
    result,
):

    if not DISCORD_WEBHOOK_URL:

        return (
            False,
            "DISCORD_WEBHOOK_URL is missing."
        )

    embed = build_embed(
        result
    )

    payload = {
        "username": "BTC Radar",
        "embeds": [
            embed
        ],
    }

    avatar_url = os.getenv(
        "DISCORD_AVATAR_URL",
        "",
    ).strip()

    if avatar_url:

        payload["avatar_url"] = (
            avatar_url
        )

    try:

        response = SESSION.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=30,
        )

        response.raise_for_status()

        return (
            True,
            None,
        )

    except requests.RequestException as error:

        return (
            False,
            str(error),
        )


# =============================================================================
# SCANNER
# =============================================================================

def scan(
    state,
):

    now = utc_now()

    current_window = (
        floor_15m_timestamp(
            now
        )
    )

    window_key = (
        datetime.fromtimestamp(
            current_window,
            tz=timezone.utc,
        ).isoformat()
    )

    state[
        "active_window"
    ] = window_key

    # -------------------------------------------------------------------------
    # ONLY SEND ONCE PER NEW 15-MINUTE CANDLE.
    #
    # IMPORTANT:
    # The clock determines when the candle opens.
    # We do NOT require Coinbase to have already published the new candle.
    # -------------------------------------------------------------------------

    if (
        state.get(
            "last_sent_window"
        )
        == window_key
    ):

        return state

    candles = (
        get_coinbase_candles()
    )

    if len(candles) < 55:

        state["last_error"] = (
            "Not enough Coinbase candles."
        )

        save_state(state)

        return state

    # -------------------------------------------------------------------------
    # GET ONLY COMPLETED CANDLES.
    #
    # Any candle whose timestamp is before the current 15-minute window
    # has already closed.
    # -------------------------------------------------------------------------

    closed_candles = [
        candle
        for candle in candles
        if candle["timestamp"] < current_window
    ]

    if len(closed_candles) < 50:

        state["last_error"] = (
            "Not enough closed candles for analysis."
        )

        save_state(state)

        return state

    # -------------------------------------------------------------------------
    # ANALYZE THE MOST RECENT COMPLETED CANDLE DATA.
    # -------------------------------------------------------------------------

    try:

        result = analyze_market(
            closed_candles
        )

    except Exception as error:

        state["last_error"] = str(
            error
        )

        logger.exception(
            "Analysis failed."
        )

        save_state(state)

        return state

    # -------------------------------------------------------------------------
    # SEND DISCORD CALL.
    # -------------------------------------------------------------------------

    success, error = (
        send_discord(
            result
        )
    )

    if success:

        state[
            "last_sent_window"
        ] = window_key

        state[
            "last_sent_at"
        ] = iso_now()

        state[
            "last_error"
        ] = None

        logger.info(
            "15m signal sent for new candle: %s | "
            "direction=%s | "
            "confidence=%s | "
            "bullish=%s | "
            "bearish=%s",
            window_key,
            result["direction"],
            result["confidence"],
            result["bullish"],
            result["bearish"],
        )

    else:

        state[
            "last_error"
        ] = (
            "Discord send failed: "
            f"{error}"
        )

        logger.error(
            "Discord send failed: %s",
            error,
        )

    state["status"] = "running"

    save_state(state)

    return state


# =============================================================================
# SHUTDOWN
# =============================================================================

def shutdown(
    signum,
    frame,
):

    global RUNNING

    RUNNING = False

    logger.info(
        "Shutdown signal received."
    )


# =============================================================================
# MAIN
# =============================================================================

def main():

    if not DISCORD_WEBHOOK_URL:

        raise SystemExit(
            "DISCORD_WEBHOOK_URL is missing. "
            "Add it to Render Environment Variables."
        )

    signal.signal(
        signal.SIGINT,
        shutdown,
    )

    signal.signal(
        signal.SIGTERM,
        shutdown,
    )

    state = load_state()

    logger.info(
        "BTC 15-Minute Market Radar started."
    )

    logger.info(
        "Data source: Coinbase BTC-USD."
    )

    logger.info(
        "Indicators: RSI, MACD, EMA9, EMA21, EMA40."
    )

    logger.info(
        "Discord timing: once at the start "
        "of every new 15-minute candle."
    )

    logger.info(
        "Candle detection uses UTC clock timing "
        "and does not depend on the new Coinbase "
        "candle appearing immediately."
    )

    # -------------------------------------------------------------------------
    # RENDER SERVER
    # -------------------------------------------------------------------------

    web_thread = Thread(
        target=run_web_server,
        daemon=True,
    )

    web_thread.start()

    logger.info(
        "Render health server started."
    )

    # -------------------------------------------------------------------------
    # RADAR LOOP
    # -------------------------------------------------------------------------

    while RUNNING:

        try:

            state = scan(
                state
            )

        except Exception as error:

            state[
                "last_error"
            ] = (
                "Unexpected scanner error: "
                f"{error}"
            )

            save_state(state)

            logger.exception(
                "Unexpected scanner error."
            )

        for _ in range(
            POLL_SECONDS
        ):

            if not RUNNING:
                break

            time.sleep(1)

    state["status"] = "stopped"

    save_state(state)

    logger.info(
        "BTC Radar stopped."
    )


# =============================================================================
# START
# =============================================================================

if __name__ == "__main__":
    main()