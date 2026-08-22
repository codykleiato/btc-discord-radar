import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ── Settings ─────────────────────────────────────────────────────────────────

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

POLL_SECONDS = 10
TIMEFRAME_MINUTES = 15

STATE_FILE = Path("btc_radar_state.json")
LOG_FILE = Path("btc_radar.log")

RUNNING = True


# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)

logger = logging.getLogger("pkla-btc-radar")


# ── HTTP retries ─────────────────────────────────────────────────────────────

def build_session() -> requests.Session:
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.25,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update(
        {
            "User-Agent": "pkla-btc-radar/1.0",
            "Accept": "application/json",
        }
    )
    return session


SESSION = build_session()


# ── State ────────────────────────────────────────────────────────────────────

def default_state() -> dict:
    return {
        "service": "pkla-btc-radar",
        "source": "Coinbase, Kraken, Bitstamp, Gemini",
        "status": "running",
        "timeframe": "15m",
        "last_open_alert_window": None,
        "last_radar_error": None,
        "last_radar_sent_at": None,
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return default_state()

    try:
        state = default_state()
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        return state
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Could not load state: %s", error)
        return default_state()


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as error:
        logger.error("Could not save state: %s", error)


# ── Time / display helpers ───────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def get_15m_window(now: datetime) -> datetime:
    minute = now.minute - (now.minute % TIMEFRAME_MINUTES)
    return now.replace(minute=minute, second=0, microsecond=0)


def money(value: Optional[float]) -> str:
    return "N/A" if value is None else f"${value:,.2f}"


def percent(value: float) -> str:
    return f"{value:+.3f}%"


# ── Exchange data ────────────────────────────────────────────────────────────

def get_coinbase_15m() -> Optional[dict]:
    try:
        now = int(time.time())

        response = SESSION.get(
            "https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD/candles",
            params={
                "start": str(now - 3600),
                "end": str(now),
                "granularity": "FIFTEEN_MINUTE",
                "limit": 10,
            },
            timeout=(5, 30),
        )
        response.raise_for_status()

        candles = response.json().get("candles", [])
        if not candles:
            raise ValueError("Coinbase returned no candles.")

        latest = max(candles, key=lambda candle: int(candle["start"]))

        return {
            "name": "Coinbase",
            "open": float(latest["open"]),
            "high": float(latest["high"]),
            "low": float(latest["low"]),
            "close": float(latest["close"]),
        }

    except (
        requests.RequestException,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        logger.warning("Coinbase failed: %s", error)
        return None


def get_kraken_15m() -> Optional[dict]:
    try:
        response = SESSION.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": "XBTUSD", "interval": 15},
            timeout=(5, 30),
        )
        response.raise_for_status()

        payload = response.json()

        if payload.get("error"):
            raise ValueError(", ".join(payload["error"]))

        result = payload["result"]
        pair_key = next(key for key in result if key != "last")
        candle = result[pair_key][-1]

        return {
            "name": "Kraken",
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
        }

    except (
        requests.RequestException,
        KeyError,
        TypeError,
        ValueError,
        StopIteration,
    ) as error:
        logger.warning("Kraken failed: %s", error)
        return None


def get_bitstamp_15m() -> Optional[dict]:
    try:
        response = SESSION.get(
            "https://www.bitstamp.net/api/v2/ohlc/btcusd/",
            params={"step": 900, "limit": 3},
            timeout=(5, 30),
        )
        response.raise_for_status()

        candles = response.json()["data"]["ohlc"]
        if not candles:
            raise ValueError("Bitstamp returned no candles.")

        latest = max(candles, key=lambda candle: int(candle["timestamp"]))

        return {
            "name": "Bitstamp",
            "open": float(latest["open"]),
            "high": float(latest["high"]),
            "low": float(latest["low"]),
            "close": float(latest["close"]),
        }

    except (
        requests.RequestException,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        logger.warning("Bitstamp failed: %s", error)
        return None


def get_gemini_15m() -> Optional[dict]:
    try:
        response = SESSION.get(
            "https://api.gemini.com/v2/candles/BTCUSD/15m",
            params={"limit": 3},
            timeout=(5, 30),
        )
        response.raise_for_status()

        candles = response.json()
        if not candles:
            raise ValueError("Gemini returned no candles.")

        # Gemini candle structure:
        # [timestamp_ms, open, high, low, close, volume]
        latest = max(candles, key=lambda candle: int(candle[0]))

        return {
            "name": "Gemini",
            "open": float(latest[1]),
            "high": float(latest[2]),
            "low": float(latest[3]),
            "close": float(latest[4]),
        }

    except (
        requests.RequestException,
        IndexError,
        TypeError,
        ValueError,
    ) as error:
        logger.warning("Gemini failed: %s", error)
        return None


def get_all_markets() -> list[dict]:
    results = [
        get_coinbase_15m(),
        get_kraken_15m(),
        get_bitstamp_15m(),
        get_gemini_15m(),
    ]
    return [market for market in results if market is not None]


# ── Four-market agreement ────────────────────────────────────────────────────

def market_direction(market: dict) -> tuple[str, float]:
    change_pct = ((market["close"] - market["open"]) / market["open"]) * 100

    if market["close"] > market["open"]:
        return "UP", change_pct

    if market["close"] < market["open"]:
        return "DOWN", change_pct

    return "FLAT", change_pct


def consensus(markets: list[dict]) -> tuple[str, int, int]:
    """
    Strict consensus:
    BET UP only if all 4 markets are up.
    BET DOWN only if all 4 markets are down.
    Otherwise, NO TRADE.
    """
    required_names = {"Coinbase", "Kraken", "Bitstamp", "Gemini"}
    available_names = {market["name"] for market in markets}

    if not required_names.issubset(available_names):
        return "NO TRADE", 0, 0

    bullish = 0
    bearish = 0

    for market in markets:
        direction, _ = market_direction(market)

        if direction == "UP":
            bullish += 1
        elif direction == "DOWN":
            bearish += 1

    if bullish == 4:
        return "UP", bullish, bearish

    if bearish == 4:
        return "DOWN", bullish, bearish

    return "NO TRADE", bullish, bearish


# ── PKLA Discord card ────────────────────────────────────────────────────────

def send_pkla_radar(
    window_start: datetime,
    markets: list[dict],
) -> tuple[bool, Optional[str]]:

    if not DISCORD_WEBHOOK_URL:
        return False, "DISCORD_WEBHOOK_URL is missing."

    signal_type, bullish, bearish = consensus(markets)

    required_names = {"Coinbase", "Kraken", "Bitstamp", "Gemini"}
    selected = [
        market
        for market in markets
        if market["name"] in required_names
    ]

    if len(selected) != 4:
        return False, "All 4 exchange feeds are required."

    average_open = sum(market["open"] for market in selected) / 4
    average_price = sum(market["close"] for market in selected) / 4
    average_change = ((average_price - average_open) / average_open) * 100

    highest_price = max(market["high"] for market in selected)
    lowest_price = min(market["low"] for market in selected)
    price_range = max(highest_price - lowest_price, 1.0)

    if signal_type == "UP":
        emoji = "🟢"
        color = 0x2ECC71
        title = "BTC UP SIGNAL"
        signal_text = "BET UP"
        take_profit = average_price + (price_range * 0.75)
        stop_loss = average_price - (price_range * 0.50)
    elif signal_type == "DOWN":
        emoji = "🔴"
        color = 0xE74C3C
        title = "BTC DOWN SIGNAL"
        signal_text = "BET DOWN"
        take_profit = average_price - (price_range * 0.75)
        stop_loss = average_price + (price_range * 0.50)
    else:
        emoji = "⚪"
        color = 0x95A5A6
        title = "BTC NO-TRADE SIGNAL"
        signal_text = "NO TRADE"
        take_profit = average_price
        stop_loss = average_price

    confidence = round((max(bullish, bearish) / 4) * 100)
    gap = bullish - bearish

    market_lines = []

    for market in sorted(selected, key=lambda item: item["name"]):
        direction, change_pct = market_direction(market)

        direction_emoji = (
            "🟢" if direction == "UP"
            else "🔴" if direction == "DOWN"
            else "⚪"
        )

        market_lines.append(
            f"{direction_emoji} {market['name']} {direction} ({percent(change_pct)})"
        )

    if signal_type == "UP":
        market_lines.insert(0, "🟢 All 4 exchanges confirm bullish direction")
    elif signal_type == "DOWN":
        market_lines.insert(0, "🔴 All 4 exchanges confirm bearish direction")
    else:
        market_lines.insert(0, "⚪ Exchanges are mixed — wait for agreement")

    sorted_markets = sorted(selected, key=lambda item: item["name"])

    indicators_text = "\n".join(
        f"{market['name']}: **{money(market['close'])}**"
        for market in sorted_markets
    )

    indicators_text += (
        f"\nAverage: **{money(average_price)}**"
        f"\nCandle change: **{percent(average_change)}**"
    )

    payload = {
        "username": "PKLA BTC Radar",
        "embeds": [
            {
                "title": f"{emoji} {title}",
                "description": (
                    "**PKLA BTC 15-Minute Market Radar**\n"
                    "Coinbase BTC-USD technical analysis"
                ),
                "color": color,
                "fields": [
                    {
                        "name": "₿ BTC PRICE",
                        "value": f"**{money(average_price)}**",
                        "inline": False,
                    },
                    {
                        "name": "📊 SIGNAL",
                        "value": f"**{signal_text}**",
                        "inline": True,
                    },
                    {
                        "name": "🎯 CONFIDENCE",
                        "value": f"**{confidence}%**",
                        "inline": True,
                    },
                    {
                        "name": "🕯️ HOLD",
                        "value": "**1 candle(s)**",
                        "inline": True,
                    },
                    {
                        "name": "📈 SCORE",
                        "value": (
                            f"Bullish: **{bullish}**\n"
                            f"Bearish: **{bearish}**\n"
                            f"Gap: **{gap:+d}**\n"
                            "Markets: **4/4**"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "📐 INDICATORS",
                        "value": indicators_text,
                        "inline": False,
                    },
                    {
                        "name": "📍 TRADE LEVELS",
                        "value": (
                            f"Entry: **{money(average_price)}**\n"
                            f"Take Profit: **{money(take_profit)}**\n"
                            f"Stop Loss: **{money(stop_loss)}**"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "🔬 ANALYSIS",
                        "value": "\n".join(market_lines),
                        "inline": False,
                    },
                    {
                        "name": "🕒 CANDLE",
                        "value": (
                            f"Opened: **{window_start.strftime('%Y-%m-%d %H:%M UTC')}**\n"
                            "Analysis: **Opening scan**"
                        ),
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": (
                        "Strict confirmation: all 4 exchanges must agree for BET UP or BET DOWN. "
                        "Informational only — not financial advice."
                    )
                },
                "timestamp": iso_now(),
            }
        ],
    }

    try:
        response = SESSION.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=(5, 30),
        )
        response.raise_for_status()
        return True, None

    except requests.RequestException as error:
        return False, str(error)


# ── Main 15-minute scanner ───────────────────────────────────────────────────

def scan_once(state: dict) -> dict:
    now = utc_now()
    window_start = get_15m_window(now)
    window_key = window_start.isoformat()

    markets = get_all_markets()

    if len(markets) != 4:
        state["last_radar_error"] = (
            f"All 4 markets are required; received {len(markets)}/4."
        )
        state["status"] = "running"
        save_state(state)
        return state

    state["last_radar_error"] = None
    state["status"] = "running"

    # One detailed PKLA message per 15-minute candle.
    if state.get("last_open_alert_window") != window_key:
        success, error = send_pkla_radar(
            window_start=window_start,
            markets=markets,
        )

        if success:
            state["last_open_alert_window"] = window_key
            state["last_radar_sent_at"] = iso_now()
            logger.info("PKLA 4-market BTC 15m radar sent.")
        else:
            state["last_radar_error"] = f"Discord webhook failed: {error}"
            logger.error("Discord webhook failed: %s", error)

    save_state(state)
    return state


def shutdown(signum, frame) -> None:
    global RUNNING
    RUNNING = False
    logger.info("Shutdown signal received.")


def main() -> None:
    if not DISCORD_WEBHOOK_URL:
        raise SystemExit(
            "DISCORD_WEBHOOK_URL is missing. Add it in Render Environment variables."
        )

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    state = load_state()

    logger.info("PKLA BTC Radar started.")
    logger.info("One detailed analysis per 15-minute candle.")
    logger.info("Strict 4-market consensus: Coinbase, Kraken, Bitstamp, Gemini.")

    while RUNNING:
        try:
            state = scan_once(state)
        except Exception as error:
            state["last_radar_error"] = f"Unexpected scanner error: {error}"
            state["status"] = "running"
            save_state(state)
            logger.exception("Unexpected scanner error")

        for _ in range(POLL_SECONDS):
            if not RUNNING:
                break
            time.sleep(1)

    state["status"] = "stopped"
    save_state(state)
    logger.info("PKLA BTC Radar stopped.")


if __name__ == "__main__":
    main()