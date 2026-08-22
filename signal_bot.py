import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Optional
from threading import Thread

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, jsonify


# =============================================================================
# REDRUM BTC 15M RADAR
#
# FOUR DISCORD CALLS PER 15-MINUTE MARKET:
#   1) START    - after 60 seconds
#   2) EARLY    - after 5 minutes
#   3) MIDPOINT - when 7:30 remains
#   4) END      - when 30 seconds remain
#
# EXCHANGE STATUS:
#   🟩 UP
#   🟥 DOWN
#   🟨 GET OUT - exchange reversed from its opening direction
#   ⬛ HOLD    - flat/no fresh directional push
#
# DATA SOURCES:
#   Coinbase, Kraken, Bitstamp
#
# RENDER:
#   Includes HTTP health server so Render detects an open port.
# =============================================================================


# ── Render health server ─────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def home():
    return jsonify(
        {
            "status": "online",
            "service": "REDRUM BTC 15M Radar",
            "timeframe": "15m",
            "data_sources": [
                "Coinbase",
                "Kraken",
                "Bitstamp",
            ],
        }
    )


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "healthy",
            "service": "REDRUM BTC 15M Radar",
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


# ── Settings ─────────────────────────────────────────────────────────────────

DISCORD_WEBHOOK_URL = os.getenv(
    "DISCORD_WEBHOOK_URL",
    "",
).strip()

POLL_SECONDS = int(
    os.getenv(
        "POLL_SECONDS",
        "10",
    )
)

TIMEFRAME_MINUTES = 15

TIMEFRAME_SECONDS = (
    TIMEFRAME_MINUTES * 60
)

# 1) START — 60 seconds after candle opens
OPEN_SCAN_DELAY_SECONDS = int(
    os.getenv(
        "OPEN_SCAN_DELAY_SECONDS",
        "60",
    )
)

# 2) EARLY — 5 minutes after candle opens
EARLY_SCAN_DELAY_SECONDS = int(
    os.getenv(
        "EARLY_SCAN_DELAY_SECONDS",
        "300",
    )
)

# 3) MIDPOINT — 7.5 minutes remaining
MAIN_CALL_REMAINING_MINUTES = float(
    os.getenv(
        "MAIN_CALL_REMAINING_MINUTES",
        "7.5",
    )
)

# 4) END — 30 seconds remaining
END_CALL_REMAINING_SECONDS = int(
    os.getenv(
        "END_CALL_REMAINING_SECONDS",
        "30",
    )
)

MIN_MAIN_CONFIDENCE = int(
    os.getenv(
        "MIN_MAIN_CONFIDENCE",
        "65",
    )
)

MIN_ABS_MOVE_PCT = float(
    os.getenv(
        "MIN_ABS_MOVE_PCT",
        "0.015",
    )
)

STATE_FILE = Path(
    os.getenv(
        "STATE_FILE",
        "redrum_btc_radar_state.json",
    )
)

LOG_FILE = Path(
    os.getenv(
        "LOG_FILE",
        "redrum_btc_radar.log",
    )
)

RUNNING = True

REQUIRED_NAMES = (
    "Coinbase",
    "Kraken",
    "Bitstamp",
)


# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            LOG_FILE,
            encoding="utf-8",
        ),
    ],
)

logger = logging.getLogger(
    "redrum-btc-radar"
)


# ── HTTP retries ─────────────────────────────────────────────────────────────

def build_session() -> requests.Session:

    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.25,
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
            "User-Agent": (
                "redrum-btc-radar/1.1"
            ),
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        }
    )

    return session


SESSION = build_session()


# ── State ────────────────────────────────────────────────────────────────────

def default_state() -> dict:

    return {
        "service": "redrum-btc-radar",
        "version": "1.1",
        "source": (
            "Coinbase, Kraken, Bitstamp"
        ),
        "status": "running",
        "timeframe": "15m",
        "active_window": None,

        # Four Discord calls
        "open_scan_sent": False,
        "early_scan_sent": False,
        "main_call_sent": False,
        "end_call_sent": False,

        "opening_directions": {},
        "last_radar_error": None,
        "last_radar_sent_at": None,
        "samples": [],
    }


def load_state() -> dict:

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

        if not isinstance(
            state.get("samples"),
            list,
        ):
            state["samples"] = []

        return state

    except (
        OSError,
        json.JSONDecodeError,
    ) as error:

        logger.warning(
            "Could not load state: %s",
            error,
        )

        return default_state()


def save_state(
    state: dict,
) -> None:

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


def reset_window_state(
    state: dict,
    window_key: str,
) -> None:

    state["active_window"] = window_key

    state["open_scan_sent"] = False
    state["early_scan_sent"] = False
    state["main_call_sent"] = False
    state["end_call_sent"] = False

    state["opening_directions"] = {}

    state["samples"] = []


# ── Time / display helpers ───────────────────────────────────────────────────

def utc_now() -> datetime:

    return datetime.now(
        timezone.utc
    )


def iso_now() -> str:

    return utc_now().isoformat()


def floor_15m_timestamp(
    now: datetime,
) -> int:

    ts = int(
        now.timestamp()
    )

    return ts - (
        ts % TIMEFRAME_SECONDS
    )


def datetime_from_ts(
    ts: int,
) -> datetime:

    return datetime.fromtimestamp(
        ts,
        tz=timezone.utc,
    )


def money(
    value: Optional[float],
) -> str:

    if value is None:
        return "N/A"

    return f"${value:,.2f}"


def percent(
    value: float,
    signed: bool = True,
) -> str:

    if signed:
        return f"{value:+.3f}%"

    return f"{value:.3f}%"


def clamp(
    value: float,
    low: float,
    high: float,
) -> float:

    return max(
        low,
        min(high, value),
    )


def safe_div(
    numerator: float,
    denominator: float,
    default: float = 0.0,
) -> float:

    if abs(denominator) < 1e-12:
        return default

    return numerator / denominator


def seconds_remaining(
    now: datetime,
    window_ts: int,
) -> float:

    return max(
        0.0,
        (
            window_ts
            + TIMEFRAME_SECONDS
        )
        - now.timestamp(),
    )


def elapsed_seconds(
    now: datetime,
    window_ts: int,
) -> float:

    return max(
        0.0,
        now.timestamp()
        - window_ts,
    )


def mmss(
    seconds: float,
) -> str:

    seconds_int = max(
        0,
        int(seconds),
    )

    minutes, secs = divmod(
        seconds_int,
        60,
    )

    return f"{minutes}:{secs:02d}"


# ── Candle normalization ─────────────────────────────────────────────────────

def candle(
    ts: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> dict:

    return {
        "ts": int(ts),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
    }


def normalize_history(
    name: str,
    candles: list[dict],
) -> dict:

    deduped = {
        int(item["ts"]): item
        for item in candles
    }

    return {
        "name": name,
        "candles": [
            deduped[key]
            for key in sorted(deduped)
        ],
    }


# ── Exchange data ────────────────────────────────────────────────────────────

def get_coinbase_15m_history() -> Optional[dict]:

    try:

        response = SESSION.get(
            (
                "https://api.exchange.coinbase.com/"
                "products/BTC-USD/candles"
            ),
            params={
                "granularity": 900
            },
            timeout=(5, 30),
        )

        response.raise_for_status()

        rows = response.json()

        if (
            not isinstance(rows, list)
            or not rows
        ):

            raise ValueError(
                "Coinbase returned no candles."
            )

        parsed = []

        for row in rows[:80]:

            if len(row) < 5:
                continue

            parsed.append(
                candle(
                    ts=int(row[0]),
                    low=float(row[1]),
                    high=float(row[2]),
                    open_=float(row[3]),
                    close=float(row[4]),
                )
            )

        if not parsed:

            raise ValueError(
                "Coinbase candle rows "
                "could not be parsed."
            )

        return normalize_history(
            "Coinbase",
            parsed,
        )

    except (
        requests.RequestException,
        TypeError,
        ValueError,
        IndexError,
    ) as error:

        logger.warning(
            "Coinbase failed: %s",
            error,
        )

        return None


def get_kraken_15m_history() -> Optional[dict]:

    try:

        response = SESSION.get(
            "https://api.kraken.com/0/public/OHLC",
            params={
                "pair": "XBTUSD",
                "interval": 15,
            },
            timeout=(5, 30),
        )

        response.raise_for_status()

        payload = response.json()

        if payload.get("error"):

            raise ValueError(
                ", ".join(
                    payload["error"]
                )
            )

        result = payload["result"]

        pair_key = next(
            key
            for key in result
            if key != "last"
        )

        rows = result[pair_key]

        parsed = []

        for row in rows[-80:]:

            if len(row) < 5:
                continue

            parsed.append(
                candle(
                    ts=int(
                        float(row[0])
                    ),
                    open_=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                )
            )

        if not parsed:

            raise ValueError(
                "Kraken returned no usable "
                "candles."
            )

        return normalize_history(
            "Kraken",
            parsed,
        )

    except (
        requests.RequestException,
        KeyError,
        TypeError,
        ValueError,
        IndexError,
        StopIteration,
    ) as error:

        logger.warning(
            "Kraken failed: %s",
            error,
        )

        return None


def get_bitstamp_15m_history() -> Optional[dict]:

    try:

        response = SESSION.get(
            (
                "https://www.bitstamp.net/"
                "api/v2/ohlc/btcusd/"
            ),
            params={
                "step": 900,
                "limit": 80,
            },
            timeout=(5, 30),
        )

        response.raise_for_status()

        rows = response.json()[
            "data"
        ][
            "ohlc"
        ]

        if not rows:

            raise ValueError(
                "Bitstamp returned no candles."
            )

        parsed = []

        for row in rows:

            parsed.append(
                candle(
                    ts=int(
                        row["timestamp"]
                    ),
                    open_=float(
                        row["open"]
                    ),
                    high=float(
                        row["high"]
                    ),
                    low=float(
                        row["low"]
                    ),
                    close=float(
                        row["close"]
                    ),
                )
            )

        return normalize_history(
            "Bitstamp",
            parsed,
        )

    except (
        requests.RequestException,
        KeyError,
        TypeError,
        ValueError,
    ) as error:

        logger.warning(
            "Bitstamp failed: %s",
            error,
        )

        return None


def get_all_market_histories() -> list[dict]:

    histories = [
        get_coinbase_15m_history(),
        get_kraken_15m_history(),
        get_bitstamp_15m_history(),
    ]

    return [
        history
        for history in histories
        if history is not None
    ]


# ── Synchronization ──────────────────────────────────────────────────────────

def candle_map(
    history: dict,
) -> dict[int, dict]:

    return {
        int(item["ts"]): item
        for item in history["candles"]
    }


def synchronize_current_markets(
    histories: list[dict],
    window_ts: int,
) -> tuple[list[dict], list[str]]:

    by_name = {
        history["name"]: history
        for history in histories
    }

    selected = []
    missing = []

    for name in REQUIRED_NAMES:

        history = by_name.get(name)

        if history is None:

            missing.append(name)

            continue

        item = candle_map(
            history
        ).get(window_ts)

        if item is None:

            missing.append(name)

            continue

        selected.append(
            {
                "name": name,
                **item,
            }
        )

    return selected, missing


def synchronized_closed_returns(
    histories: list[dict],
    current_window_ts: int,
    count: int = 6,
) -> list[dict]:

    by_name = {
        history["name"]:
        candle_map(history)
        for history in histories
    }

    rows = []

    for offset in range(
        count,
        0,
        -1,
    ):

        ts = (
            current_window_ts
            - (
                offset
                * TIMEFRAME_SECONDS
            )
        )

        exchange_candles = []

        for name in REQUIRED_NAMES:

            item = by_name.get(
                name,
                {},
            ).get(ts)

            if item is None:

                exchange_candles = []

                break

            exchange_candles.append(
                item
            )

        if len(exchange_candles) != len(
            REQUIRED_NAMES
        ):
            continue

        avg_open = mean(
            item["open"]
            for item in exchange_candles
        )

        avg_close = mean(
            item["close"]
            for item in exchange_candles
        )

        avg_high = mean(
            item["high"]
            for item in exchange_candles
        )

        avg_low = mean(
            item["low"]
            for item in exchange_candles
        )

        rows.append(
            {
                "ts": ts,
                "open": avg_open,
                "high": avg_high,
                "low": avg_low,
                "close": avg_close,
                "change_pct": safe_div(
                    avg_close - avg_open,
                    avg_open,
                ) * 100,
            }
        )

    return rows


# ── Signal engine ────────────────────────────────────────────────────────────

def market_direction(
    market: dict,
) -> tuple[str, float]:

    change_pct = (
        safe_div(
            market["close"]
            - market["open"],
            market["open"],
        )
        * 100
    )

    if market["close"] > market["open"]:

        return "UP", change_pct

    if market["close"] < market["open"]:

        return "DOWN", change_pct

    return "FLAT", change_pct


def consensus(
    markets: list[dict],
) -> tuple[str, int, int, int]:

    if (
        len(markets)
        != len(REQUIRED_NAMES)
        or {
            m["name"]
            for m in markets
        }
        != set(REQUIRED_NAMES)
    ):

        return (
            "NO TRADE",
            0,
            0,
            0,
        )

    bullish = 0
    bearish = 0
    flat = 0

    for market in markets:

        direction, _ = (
            market_direction(market)
        )

        if direction == "UP":

            bullish += 1

        elif direction == "DOWN":

            bearish += 1

        else:

            flat += 1

    required = len(
        REQUIRED_NAMES
    )

    if bullish == required:

        return (
            "UP",
            bullish,
            bearish,
            flat,
        )

    if bearish == required:

        return (
            "DOWN",
            bullish,
            bearish,
            flat,
        )

    return (
        "NO TRADE",
        bullish,
        bearish,
        flat,
    )


def aggregate_current(
    markets: list[dict],
) -> dict:

    avg_open = mean(
        m["open"]
        for m in markets
    )

    avg_high = mean(
        m["high"]
        for m in markets
    )

    avg_low = mean(
        m["low"]
        for m in markets
    )

    avg_close = mean(
        m["close"]
        for m in markets
    )

    change_pct = (
        safe_div(
            avg_close - avg_open,
            avg_open,
        )
        * 100
    )

    range_dollars = max(
        avg_high - avg_low,
        0.01,
    )

    body_dollars = abs(
        avg_close - avg_open
    )

    body_ratio = clamp(
        safe_div(
            body_dollars,
            range_dollars,
        ),
        0.0,
        1.0,
    )

    if avg_close >= avg_open:

        location = clamp(
            safe_div(
                avg_close - avg_low,
                range_dollars,
            ),
            0.0,
            1.0,
        )

    else:

        location = clamp(
            safe_div(
                avg_high - avg_close,
                range_dollars,
            ),
            0.0,
            1.0,
        )

    prices = [
        m["close"]
        for m in markets
    ]

    cross_spread_pct = (
        safe_div(
            max(prices) - min(prices),
            avg_close,
        )
        * 100
    )

    return {
        "open": avg_open,
        "high": avg_high,
        "low": avg_low,
        "price": avg_close,
        "change_pct": change_pct,
        "range_dollars": range_dollars,
        "body_ratio": body_ratio,
        "location": location,
        "cross_spread_pct": cross_spread_pct,
    }


def add_sample(
    state: dict,
    window_key: str,
    current: dict,
    now: datetime,
) -> None:

    samples = state.setdefault(
        "samples",
        [],
    )

    if (
        samples
        and samples[-1].get("window")
        != window_key
    ):

        samples.clear()

    samples.append(
        {
            "window": window_key,
            "ts": now.timestamp(),
            "price": current["price"],
            "change_pct": current["change_pct"],
        }
    )

    cutoff = (
        now.timestamp()
        - 300
    )

    state["samples"] = [
        sample
        for sample in samples
        if sample.get("ts", 0)
        >= cutoff
    ][-40:]


def momentum_from_samples(
    state: dict,
    now: datetime,
    lookback_seconds: int = 60,
) -> dict:

    samples = state.get(
        "samples",
        [],
    )

    if len(samples) < 2:

        return {
            "delta": 0.0,
            "delta_pct": 0.0,
            "direction": "FLAT",
        }

    latest = samples[-1]

    target_ts = (
        now.timestamp()
        - lookback_seconds
    )

    prior = min(
        samples[:-1],
        key=lambda sample:
        abs(
            sample["ts"]
            - target_ts
        ),
    )

    delta = (
        latest["price"]
        - prior["price"]
    )

    delta_pct = (
        safe_div(
            delta,
            prior["price"],
        )
        * 100
    )

    if delta > 0:

        direction = "UP"

    elif delta < 0:

        direction = "DOWN"

    else:

        direction = "FLAT"

    return {
        "delta": delta,
        "delta_pct": delta_pct,
        "direction": direction,
    }


def previous_trend(
    closed: list[dict],
) -> dict:

    if not closed:

        return {
            "direction": "FLAT",
            "score": 50,
            "avg_change_pct": 0.0,
        }

    recent = closed[-3:]

    avg_change = mean(
        row["change_pct"]
        for row in recent
    )

    up_count = sum(
        1
        for row in recent
        if row["change_pct"] > 0
    )

    down_count = sum(
        1
        for row in recent
        if row["change_pct"] < 0
    )

    if up_count > down_count:

        direction = "UP"

    elif down_count > up_count:

        direction = "DOWN"

    else:

        direction = "FLAT"

    directional_ratio = (
        max(
            up_count,
            down_count,
        )
        / max(
            len(recent),
            1,
        )
    )

    magnitude = clamp(
        abs(avg_change) / 0.10,
        0.0,
        1.0,
    )

    score = round(
        clamp(
            50
            + (
                directional_ratio
                * 25
            )
            + (
                magnitude
                * 20
            ),
            50,
            95,
        )
    )

    return {
        "direction": direction,
        "score": score,
        "avg_change_pct": avg_change,
    }


def model_metrics(
    markets: list[dict],
    current: dict,
    momentum: dict,
    closed: list[dict],
) -> dict:

    (
        signal_type,
        bullish,
        bearish,
        flat,
    ) = consensus(markets)

    dominant = (
        "UP"
        if bullish > bearish
        else "DOWN"
        if bearish > bullish
        else "FLAT"
    )

    agreement_pct = round(
        (
            max(
                bullish,
                bearish,
            )
            / len(REQUIRED_NAMES)
        )
        * 100
    )

    move_strength = (
        clamp(
            abs(
                current["change_pct"]
            )
            / 0.10,
            0.0,
            1.0,
        )
        * 100
    )

    body_strength = (
        current["body_ratio"]
        * 100
    )

    candle_location = (
        current["location"]
        * 100
    )

    momentum_aligned = (
        dominant
        in ("UP", "DOWN")
        and momentum["direction"]
        == dominant
    )

    momentum_opposed = (
        dominant
        in ("UP", "DOWN")
        and momentum["direction"]
        in ("UP", "DOWN")
        and momentum["direction"]
        != dominant
    )

    momentum_strength = (
        clamp(
            abs(
                momentum["delta_pct"]
            )
            / 0.05,
            0.0,
            1.0,
        )
        * 100
    )

    if momentum_aligned:

        momentum_component = (
            55
            + (
                0.45
                * momentum_strength
            )
        )

    elif momentum_opposed:

        momentum_component = (
            45
            - (
                0.45
                * momentum_strength
            )
        )

    else:

        momentum_component = 50

    trend = previous_trend(
        closed
    )

    trend_aligned = (
        dominant
        in ("UP", "DOWN")
        and trend["direction"]
        == dominant
    )

    trend_component = (
        trend["score"]
        if trend_aligned
        else (
            100 - trend["score"]
            if trend["direction"]
            != "FLAT"
            else 50
        )
    )

    raw_confidence = (
        0.30
        * agreement_pct
        + 0.20
        * move_strength
        + 0.15
        * body_strength
        + 0.15
        * candle_location
        + 0.10
        * momentum_component
        + 0.10
        * trend_component
    )

    spread_penalty = clamp(
        (
            current[
                "cross_spread_pct"
            ]
            - 0.03
        )
        * 200,
        0,
        10,
    )

    confidence = round(
        clamp(
            raw_confidence
            - spread_penalty,
            1,
            95,
        )
    )

    proximity_to_open_pct = abs(
        current["change_pct"]
    )

    near_open_risk = (
        100
        * (
            1
            - clamp(
                proximity_to_open_pct
                / 0.08,
                0.0,
                1.0,
            )
        )
    )

    opposing_momentum_risk = (
        momentum_strength
        if momentum_opposed
        else 0
    )

    weak_body_risk = (
        100
        - body_strength
    )

    mixed_exchange_risk = (
        100
        - agreement_pct
    )

    flip_risk = round(
        clamp(
            0.40
            * near_open_risk
            + 0.30
            * opposing_momentum_risk
            + 0.20
            * weak_body_risk
            + 0.10
            * mixed_exchange_risk,
            0,
            100,
        )
    )

    next_direction = "WAIT"
    next_score = 50

    directional_votes = 0

    if signal_type == "UP":

        directional_votes += 2

    elif signal_type == "DOWN":

        directional_votes -= 2

    if momentum["direction"] == "UP":

        directional_votes += 1

    elif momentum["direction"] == "DOWN":

        directional_votes -= 1

    if trend["direction"] == "UP":

        directional_votes += 1

    elif trend["direction"] == "DOWN":

        directional_votes -= 1

    if current["change_pct"] > 0.03:

        directional_votes += 1

    elif current["change_pct"] < -0.03:

        directional_votes -= 1

    if directional_votes >= 3:

        next_direction = "UP"

    elif directional_votes <= -3:

        next_direction = "DOWN"

    next_score = round(
        clamp(
            50
            + (
                abs(
                    directional_votes
                )
                * 8
            ),
            50,
            90,
        )
    )

    return {
        "strict_signal": signal_type,
        "dominant": dominant,
        "bullish": bullish,
        "bearish": bearish,
        "flat": flat,
        "agreement_pct": agreement_pct,
        "confidence": confidence,
        "flip_risk": flip_risk,
        "momentum": momentum,
        "trend": trend,
        "next_direction": next_direction,
        "next_score": next_score,
    }


def qualified_main_call(
    metrics: dict,
    current: dict,
) -> str:

    strict_signal = metrics[
        "strict_signal"
    ]

    if strict_signal not in (
        "UP",
        "DOWN",
    ):

        return "WAIT"

    if (
        metrics["confidence"]
        < MIN_MAIN_CONFIDENCE
    ):

        return "WAIT"

    if (
        abs(current["change_pct"])
        < MIN_ABS_MOVE_PCT
    ):

        return "WAIT"

    return strict_signal


# ── Discord helpers ──────────────────────────────────────────────────────────

def discord_style(
    direction: str,
) -> tuple[str, int]:

    if direction == "UP":

        return (
            "🟢",
            0x2ECC71,
        )

    if direction == "DOWN":

        return (
            "🔴",
            0xE74C3C,
        )

    return (
        "⚪",
        0x95A5A6,
    )


def exchange_status(
    market: dict,
    stage: str,
    opening_directions: dict,
) -> tuple[str, str]:

    direction, change_pct = (
        market_direction(market)
    )

    if stage in (
        "EARLY",
        "MID",
        "END",
    ):

        opening = (
            opening_directions.get(
                market["name"]
            )
        )

        if (
            opening
            in ("UP", "DOWN")
            and direction
            in ("UP", "DOWN")
            and direction
            != opening
        ):

            return (
                "🟨",
                "GET OUT",
            )

    if direction == "UP":

        return (
            "🟩",
            "UP",
        )

    if direction == "DOWN":

        return (
            "🟥",
            "DOWN",
        )

    return (
        "⬛",
        "HOLD",
    )


def exchange_lines(
    markets: list[dict],
    stage: str,
    opening_directions: dict,
) -> str:

    lines = []

    for market in sorted(
        markets,
        key=lambda item:
        item["name"],
    ):

        _, change_pct = (
            market_direction(market)
        )

        icon, status = exchange_status(
            market,
            stage,
            opening_directions,
        )

        lines.append(
            f"{icon} **{market['name']}** — "
            f"**{status}** "
            f"{percent(change_pct)} | "
            f"{money(market['close'])}"
        )

    return "\n".join(lines)


def send_discord_embed(
    embed: dict,
) -> tuple[bool, Optional[str]]:

    if not DISCORD_WEBHOOK_URL:

        return (
            False,
            "DISCORD_WEBHOOK_URL is missing.",
        )

    payload = {
        "username": "REDRUM BTC Radar",
        "embeds": [embed],
    }

    try:

        response = SESSION.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=(5, 30),
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


def make_embed(
    stage: str,
    window_ts: int,
    remaining: float,
    markets: list[dict],
    current: dict,
    metrics: dict,
    opening_directions: dict,
) -> dict:

    main_call = qualified_main_call(
        metrics,
        current,
    )

    # ── Stage-specific call display ──────────────────────────────────────────

    if stage == "OPEN":

        displayed_direction = (
            metrics["strict_signal"]
        )

        call_text = (
            "START CALL — HOLD"
            if displayed_direction
            == "NO TRADE"
            else
            f"START CALL — "
            f"{displayed_direction}"
        )

        title_prefix = (
            "START — CANDLE OPEN"
        )

    elif stage == "EARLY":

        displayed_direction = (
            metrics["strict_signal"]
        )

        call_text = (
            "EARLY CALL — HOLD"
            if displayed_direction
            == "NO TRADE"
            else
            f"EARLY CALL — "
            f"{displayed_direction}"
        )

        title_prefix = (
            "EARLY — 5 MINUTES"
        )

    elif stage == "MID":

        displayed_direction = (
            main_call
            if main_call != "WAIT"
            else "WAIT"
        )

        call_text = (
            f"MIDPOINT CALL — "
            f"{main_call}"
            if main_call
            in ("UP", "DOWN")
            else
            "MIDPOINT CALL — "
            "HOLD / WAIT"
        )

        title_prefix = (
            "MIDPOINT — 7:30 LEFT"
        )

    elif stage == "END":

        displayed_direction = (
            main_call
            if main_call != "WAIT"
            else
            metrics["strict_signal"]
        )

        call_text = (
            f"END CALL — "
            f"{displayed_direction}"
            if displayed_direction
            in ("UP", "DOWN")
            else
            "END CALL — HOLD / WAIT"
        )

        title_prefix = (
            "END — 30 SECONDS LEFT"
        )

    else:

        displayed_direction = "WAIT"

        call_text = (
            "REDRUM — WAIT"
        )

        title_prefix = "STATUS"

    emoji, color = discord_style(
        displayed_direction
    )

    momentum = metrics[
        "momentum"
    ]

    trend = metrics[
        "trend"
    ]

    price_vs_open = (
        current["price"]
        - current["open"]
    )

    if metrics["next_direction"] in (
        "UP",
        "DOWN",
    ):

        next_text = (
            f"{metrics['next_direction']} "
            f"({metrics['next_score']} "
            f"heuristic score)"
        )

    else:

        next_text = "WAIT"

    return {

        "title": (
            f"{emoji} REDRUM BTC 15M — "
            f"{title_prefix}"
        ),

        "description": (
            f"**{call_text}**\n"
            "Four-stage synchronized "
            "BTC/USD radar"
        ),

        "color": color,

        "fields": [

            {
                "name": "₿ BTC PRICE",
                "value": (
                    f"Average: **"
                    f"{money(current['price'])}"
                    f"**\n"
                    f"15m Open: **"
                    f"{money(current['open'])}"
                    f"**\n"
                    f"Vs Open: **"
                    f"{price_vs_open:+,.2f} "
                    f"({percent(current['change_pct'])})"
                    f"**"
                ),
                "inline": False,
            },

            {
                "name": "📣 CURRENT CALL",
                "value": (
                    f"**{call_text}**"
                ),
                "inline": True,
            },

            {
                "name": "🧠 MODEL CONFIDENCE",
                "value": (
                    f"**{metrics['confidence']}%**\n"
                    "*heuristic, not win probability*"
                ),
                "inline": True,
            },

            {
                "name": "⚠️ FLIP RISK",
                "value": (
                    f"**{metrics['flip_risk']}%**"
                ),
                "inline": True,
            },

            {
                "name": "⏱️ TIME LEFT",
                "value": (
                    f"**{mmss(remaining)}**"
                ),
                "inline": True,
            },

            {
                "name": "🤝 EXCHANGE AGREEMENT",
                "value": (
                    f"Bullish: **"
                    f"{metrics['bullish']}/"
                    f"{len(REQUIRED_NAMES)}**\n"
                    f"Bearish: **"
                    f"{metrics['bearish']}/"
                    f"{len(REQUIRED_NAMES)}**\n"
                    f"Agreement: **"
                    f"{metrics['agreement_pct']}%**\n"
                    f"Strict signal: **"
                    f"{metrics['strict_signal']}**"
                ),
                "inline": True,
            },

            {
                "name": "⚡ 60s MOMENTUM",
                "value": (
                    f"Direction: **"
                    f"{momentum['direction']}**\n"
                    f"Move: **"
                    f"{momentum['delta']:+,.2f} "
                    f"({percent(momentum['delta_pct'])})"
                    f"**"
                ),
                "inline": True,
            },

            {
                "name": "📚 PREVIOUS 3-CANDLE TREND",
                "value": (
                    f"Direction: **"
                    f"{trend['direction']}**\n"
                    f"Avg change: **"
                    f"{percent(trend['avg_change_pct'])}"
                    f"**"
                ),
                "inline": True,
            },

            {
                "name": "🔮 NEXT CANDLE LEAN",
                "value": (
                    f"**{next_text}**"
                ),
                "inline": True,
            },

            {
                "name": "🏦 EXCHANGE COLOR CODE",
                "value": exchange_lines(
                    markets,
                    stage,
                    opening_directions,
                ),
                "inline": False,
            },

            {
                "name": "🎨 COLOR KEY",
                "value": (
                    "🟩 **UP**  |  "
                    "🟥 **DOWN**  |  "
                    "🟨 **GET OUT**  |  "
                    "⬛ **HOLD**"
                ),
                "inline": False,
            },

            {
                "name": "🕒 CANDLE",
                "value": (
                    f"Opened: **"
                    f"{datetime_from_ts(window_ts).strftime('%Y-%m-%d %H:%M UTC')}"
                    f"**\n"
                    f"Stage: **{stage}**"
                ),
                "inline": False,
            },
        ],

        "footer": {
            "text": (
                "REDRUM uses synchronized "
                "Coinbase, Kraken, and Bitstamp "
                "plus quality gates. Model "
                "confidence is a heuristic signal "
                "score, not a guaranteed probability."
            )
        },

        "timestamp": iso_now(),
    }


def send_stage(
    stage: str,
    window_ts: int,
    remaining: float,
    markets: list[dict],
    current: dict,
    metrics: dict,
    opening_directions: dict,
) -> tuple[bool, Optional[str]]:

    embed = make_embed(
        stage=stage,
        window_ts=window_ts,
        remaining=remaining,
        markets=markets,
        current=current,
        metrics=metrics,
        opening_directions=opening_directions,
    )

    return send_discord_embed(
        embed
    )


# ── Main scanner ─────────────────────────────────────────────────────────────

def scan_once(
    state: dict,
) -> dict:

    now = utc_now()

    window_ts = floor_15m_timestamp(
        now
    )

    window_key = (
        datetime_from_ts(
            window_ts
        ).isoformat()
    )

    # ── New candle ───────────────────────────────────────────────────────────

    if (
        state.get("active_window")
        != window_key
    ):

        reset_window_state(
            state,
            window_key,
        )

        logger.info(
            "New 15m candle detected: %s",
            window_key,
        )

    # ── Get exchange histories ───────────────────────────────────────────────

    histories = (
        get_all_market_histories()
    )

    history_names = {
        history["name"]
        for history in histories
    }

    if (
        history_names
        != set(REQUIRED_NAMES)
    ):

        missing = sorted(
            set(REQUIRED_NAMES)
            - history_names
        )

        state["last_radar_error"] = (
            "Missing exchange histories: "
            + ", ".join(missing)
        )

        state["status"] = "running"

        save_state(state)

        return state

    markets, missing_current = (
        synchronize_current_markets(
            histories,
            window_ts,
        )
    )

    if missing_current:

        state["last_radar_error"] = (
            "Current 15m candle is not "
            "synchronized yet for: "
            + ", ".join(missing_current)
        )

        state["status"] = "running"

        save_state(state)

        return state

    # ── Current market calculations ──────────────────────────────────────────

    current = aggregate_current(
        markets
    )

    add_sample(
        state,
        window_key,
        current,
        now,
    )

    momentum = momentum_from_samples(
        state,
        now,
        lookback_seconds=60,
    )

    closed = (
        synchronized_closed_returns(
            histories,
            window_ts,
            count=6,
        )
    )

    metrics = model_metrics(
        markets,
        current,
        momentum,
        closed,
    )

    elapsed = elapsed_seconds(
        now,
        window_ts,
    )

    remaining = seconds_remaining(
        now,
        window_ts,
    )

    state["last_radar_error"] = None
    state["status"] = "running"

    # =========================================================================
    # 1) START CALL — 60 seconds after candle opens
    # =========================================================================

    if (
        not state.get(
            "open_scan_sent",
            False,
        )
        and elapsed
        >= OPEN_SCAN_DELAY_SECONDS
    ):

        success, error = send_stage(
            "OPEN",
            window_ts,
            remaining,
            markets,
            current,
            metrics,
            state.get(
                "opening_directions",
                {},
            ),
        )

        if success:

            state[
                "open_scan_sent"
            ] = True

            # Capture the opening direction
            # for GET OUT detection later.
            state[
                "opening_directions"
            ] = {
                m["name"]:
                market_direction(m)[0]
                for m in markets
            }

            state[
                "last_radar_sent_at"
            ] = iso_now()

            logger.info(
                "START call sent for %s",
                window_key,
            )

        else:

            state[
                "last_radar_error"
            ] = (
                "Discord START failed: "
                f"{error}"
            )

            logger.error(
                "Discord START failed: %s",
                error,
            )

    # =========================================================================
    # 2) EARLY CALL — 5 minutes after candle opens
    # =========================================================================

    if (
        not state.get(
            "early_scan_sent",
            False,
        )
        and elapsed
        >= EARLY_SCAN_DELAY_SECONDS
    ):

        success, error = send_stage(
            "EARLY",
            window_ts,
            remaining,
            markets,
            current,
            metrics,
            state.get(
                "opening_directions",
                {},
            ),
        )

        if success:

            state[
                "early_scan_sent"
            ] = True

            state[
                "last_radar_sent_at"
            ] = iso_now()

            logger.info(
                "EARLY call sent for %s: "
                "%s confidence=%s flip=%s",
                window_key,
                qualified_main_call(
                    metrics,
                    current,
                ),
                metrics["confidence"],
                metrics["flip_risk"],
            )

        else:

            state[
                "last_radar_error"
            ] = (
                "Discord EARLY failed: "
                f"{error}"
            )

            logger.error(
                "Discord EARLY failed: %s",
                error,
            )

    # =========================================================================
    # 3) MIDPOINT CALL — 7:30 remaining
    # =========================================================================

    main_threshold_seconds = (
        MAIN_CALL_REMAINING_MINUTES
        * 60
    )

    if (
        not state.get(
            "main_call_sent",
            False,
        )
        and remaining
        <= main_threshold_seconds
    ):

        success, error = send_stage(
            "MID",
            window_ts,
            remaining,
            markets,
            current,
            metrics,
            state.get(
                "opening_directions",
                {},
            ),
        )

        if success:

            state[
                "main_call_sent"
            ] = True

            state[
                "last_radar_sent_at"
            ] = iso_now()

            logger.info(
                "MIDPOINT call sent for %s: "
                "%s confidence=%s flip=%s",
                window_key,
                qualified_main_call(
                    metrics,
                    current,
                ),
                metrics["confidence"],
                metrics["flip_risk"],
            )

        else:

            state[
                "last_radar_error"
            ] = (
                "Discord MIDPOINT failed: "
                f"{error}"
            )

            logger.error(
                "Discord MIDPOINT failed: %s",
                error,
            )

    # =========================================================================
    # 4) END CALL — 30 seconds remaining
    # =========================================================================

    if (
        not state.get(
            "end_call_sent",
            False,
        )
        and remaining
        <= END_CALL_REMAINING_SECONDS
    ):

        success, error = send_stage(
            "END",
            window_ts,
            remaining,
            markets,
            current,
            metrics,
            state.get(
                "opening_directions",
                {},
            ),
        )

        if success:

            state[
                "end_call_sent"
            ] = True

            state[
                "last_radar_sent_at"
            ] = iso_now()

            logger.info(
                "END call sent for %s: "
                "%s confidence=%s flip=%s",
                window_key,
                qualified_main_call(
                    metrics,
                    current,
                ),
                metrics["confidence"],
                metrics["flip_risk"],
            )

        else:

            state[
                "last_radar_error"
            ] = (
                "Discord END failed: "
                f"{error}"
            )

            logger.error(
                "Discord END failed: %s",
                error,
            )

    save_state(state)

    return state


def shutdown(
    signum,
    frame,
) -> None:

    global RUNNING

    RUNNING = False

    logger.info(
        "Shutdown signal received."
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:

    if not DISCORD_WEBHOOK_URL:

        raise SystemExit(
            "DISCORD_WEBHOOK_URL is missing. "
            "Add it to your environment variables."
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
        "REDRUM BTC Radar started."
    )

    logger.info(
        "15m synchronized: "
        "Coinbase, Kraken, Bitstamp."
    )

    logger.info(
        "Four calls per candle: "
        "START after %ss | "
        "EARLY after %ss | "
        "MIDPOINT at <= %.1fm left | "
        "END at <= %ss remaining",
        OPEN_SCAN_DELAY_SECONDS,
        EARLY_SCAN_DELAY_SECONDS,
        MAIN_CALL_REMAINING_MINUTES,
        END_CALL_REMAINING_SECONDS,
    )

    logger.info(
        "Main quality gates: 3/3 strict + "
        "confidence >= %s + "
        "abs move >= %.3f%%",
        MIN_MAIN_CONFIDENCE,
        MIN_ABS_MOVE_PCT,
    )

    # ── START RENDER HEALTH SERVER ───────────────────────────────────────────

    web_thread = Thread(
        target=run_web_server,
        daemon=True,
    )

    web_thread.start()

    logger.info(
        "Render health server started."
    )

    # ── START RADAR ──────────────────────────────────────────────────────────

    while RUNNING:

        try:

            state = scan_once(
                state
            )

        except Exception as error:

            state[
                "last_radar_error"
            ] = (
                "Unexpected scanner error: "
                f"{error}"
            )

            state["status"] = "running"

            save_state(state)

            logger.exception(
                "Unexpected scanner error"
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
        "REDRUM BTC Radar stopped."
    )


if __name__ == "__main__":

    main()