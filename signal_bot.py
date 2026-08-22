#!/usr/bin/env python3
"""
btc_discord_radar.py

BTC 15-minute Discord radar using Coinbase BTC-USD spot price.

Install:
    pip install requests

Run:
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN"
    python btc_discord_radar.py
"""

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


# ── Configuration ────────────────────────────────────────────────────────────

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

COINBASE_PAIR = "BTC-USD"
COINBASE_URL = f"https://api.coinbase.com/v2/prices/{COINBASE_PAIR}/spot"

POLL_SECONDS = 10
TIMEFRAME_MINUTES = 15

# Set this to match the exact target listed on a Kalshi market.
# Example: If the listed target is $25 above the 15m candle open, use 25.0.
KALSHI_TARGET_OFFSET_USD = 0.0

# Signal filters. Set to False for simpler target-only alerts.
REQUIRE_CANDLE_DIRECTION = True
REQUIRE_EMA_TREND = True

FAST_EMA_LENGTH = 9
SLOW_EMA_LENGTH = 21

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
logger = logging.getLogger("btc-discord-radar")


# ── HTTP session with retry protection ───────────────────────────────────────

def build_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.5,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )

    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "btc-discord-radar/1.0",
            "Accept": "application/json",
        }
    )
    return session


SESSION = build_session()


# ── Persistent state ─────────────────────────────────────────────────────────

def default_state() -> dict:
    return {
        "service": "btc-discord-radar",
        "status": "running",
        "source": "Coinbase BTC-USD",
        "timeframe": "15m",
        "current_window_start": None,
        "window_open_price": None,
        "kalshi_target": None,
        "last_price": None,
        "last_signal": None,
        "last_signal_window": None,
        "last_radar_sent_at": None,
        "last_radar_error": None,
        "ema_prices": [],
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return default_state()

    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state = default_state()
        state.update(saved)
        return state
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Could not load state; starting fresh: %s", error)
        return default_state()


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as error:
        logger.error("Could not save state: %s", error)


# ── Time and price helpers ───────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def fifteen_minute_window(now: datetime) -> datetime:
    minute = now.minute - (now.minute % TIMEFRAME_MINUTES)
    return now.replace(minute=minute, second=0, microsecond=0)


def format_usd(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"${value:,.2f}"


def get_btc_price() -> Optional[float]:
    try:
        response = SESSION.get(
            COINBASE_URL,
            timeout=(5, 45),  # connect timeout, read timeout
        )
        response.raise_for_status()

        payload = response.json()
        price = float(payload["data"]["amount"])

        if price <= 0:
            raise ValueError(f"Invalid BTC price received: {price}")

        return price

    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
        logger.warning("Coinbase network timeout/connection error: %s", error)
        return None

    except (requests.exceptions.RequestException, KeyError, TypeError, ValueError) as error:
        logger.warning("Coinbase price request failed: %s", error)
        return None


# ── EMA logic ────────────────────────────────────────────────────────────────

def calculate_ema(values: list[float], length: int) -> Optional[float]:
    if len(values) < length:
        return None

    multiplier = 2 / (length + 1)
    ema = values[0]

    for value in values[1:]:
        ema = (value - ema) * multiplier + ema

    return ema


def update_price_history(state: dict, price: float) -> None:
    prices = state.get("ema_prices", [])
    prices.append(price)

    # Keep enough samples for both EMAs but avoid unlimited state-file growth.
    max_samples = max(SLOW_EMA_LENGTH * 4, 100)
    state["ema_prices"] = prices[-max_samples:]


# ── Discord ──────────────────────────────────────────────────────────────────

def send_discord_alert(
    signal_type: str,
    price: float,
    target: float,
    window_start: datetime,
    fast_ema: Optional[float],
    slow_ema: Optional[float],
) -> tuple[bool, Optional[str]]:

    if not DISCORD_WEBHOOK_URL:
        return False, "DISCORD_WEBHOOK_URL is not set"

    is_buy = signal_type == "BUY"
    color = 0x2ECC71 if is_buy else 0xE74C3C
    direction = "ABOVE" if is_buy else "BELOW"
    emoji = "🟢" if is_buy else "🔴"

    description = (
        f"{emoji} **{signal_type} RADAR**\n"
        f"BTC is currently **{direction}** the 15-minute target."
    )

    payload = {
        "username": "BTC 15m Radar",
        "embeds": [
            {
                "title": f"BTC-USD 15m {signal_type} Signal",
                "description": description,
                "color": color,
                "fields": [
                    {
                        "name": "Current BTC Price",
                        "value": format_usd(price),
                        "inline": True,
                    },
                    {
                        "name": "15m Target",
                        "value": format_usd(target),
                        "inline": True,
                    },
                    {
                        "name": "Difference",
                        "value": format_usd(abs(price - target)),
                        "inline": True,
                    },
                    {
                        "name": "15m Window Start (UTC)",
                        "value": window_start.strftime("%Y-%m-%d %H:%M"),
                        "inline": False,
                    },
                    {
                        "name": "Fast EMA / Slow EMA",
                        "value": (
                            f"{format_usd(fast_ema)} / {format_usd(slow_ema)}"
                            if fast_ema is not None and slow_ema is not None
                            else "Warming up"
                        ),
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": "Coinbase BTC-USD spot | Signal is informational only",
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

    except requests.exceptions.RequestException as error:
        return False, str(error)


# ── Signal logic ─────────────────────────────────────────────────────────────

def determine_signal(
    price: float,
    target: float,
    window_open_price: float,
    fast_ema: Optional[float],
    slow_ema: Optional[float],
) -> Optional[str]:
    """
    BUY: BTC is above the target, current 15m candle is green,
         and optional EMA trend is bullish.

    SELL: BTC is below the target, current 15m candle is red,
          and optional EMA trend is bearish.
    """
    above_target = price > target
    below_target = price < target

    candle_green = price > window_open_price
    candle_red = price < window_open_price

    ema_bullish = (
        fast_ema is not None
        and slow_ema is not None
        and fast_ema > slow_ema
    )
    ema_bearish = (
        fast_ema is not None
        and slow_ema is not None
        and fast_ema < slow_ema
    )

    buy_ok = above_target
    sell_ok = below_target

    if REQUIRE_CANDLE_DIRECTION:
        buy_ok = buy_ok and candle_green
        sell_ok = sell_ok and candle_red

    if REQUIRE_EMA_TREND:
        buy_ok = buy_ok and ema_bullish
        sell_ok = sell_ok and ema_bearish

    if buy_ok:
        return "BUY"

    if sell_ok:
        return "SELL"

    return None


# ── Main scanner ─────────────────────────────────────────────────────────────

def scan_once(state: dict) -> dict:
    now = utc_now()
    window_start = fifteen_minute_window(now)
    window_key = window_start.isoformat()

    price = get_btc_price()

    if price is None:
        state["last_radar_error"] = (
            "Could not retrieve Coinbase BTC-USD price; retrying next scan."
        )
        state["status"] = "running"
        save_state(state)
        return state

    state["last_price"] = price
    state["last_radar_error"] = None

    # Start/reset a target when the next 15-minute window begins.
    if state.get("current_window_start") != window_key:
        state["current_window_start"] = window_key
        state["window_open_price"] = price
        state["kalshi_target"] = price + KALSHI_TARGET_OFFSET_USD
        state["last_signal"] = None
        state["last_signal_window"] = None

        logger.info(
            "New 15m window: open=%s target=%s",
            format_usd(price),
            format_usd(state["kalshi_target"]),
        )

    update_price_history(state, price)

    fast_ema = calculate_ema(state["ema_prices"], FAST_EMA_LENGTH)
    slow_ema = calculate_ema(state["ema_prices"], SLOW_EMA_LENGTH)

    target = float(state["kalshi_target"])
    window_open_price = float(state["window_open_price"])

    signal_type = determine_signal(
        price=price,
        target=target,
        window_open_price=window_open_price,
        fast_ema=fast_ema,
        slow_ema=slow_ema,
    )

    logger.info(
        "BTC=%s | target=%s | EMA%s=%s | EMA%s=%s | signal=%s",
        format_usd(price),
        format_usd(target),
        FAST_EMA_LENGTH,
        format_usd(fast_ema),
        SLOW_EMA_LENGTH,
        format_usd(slow_ema),
        signal_type or "NONE",
    )

    # One alert maximum per signal type, per 15-minute window.
    already_sent = (
        state.get("last_signal") == signal_type
        and state.get("last_signal_window") == window_key
    )

    if signal_type and not already_sent:
        success, error = send_discord_alert(
            signal_type=signal_type,
            price=price,
            target=target,
            window_start=window_start,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
        )

        if success:
            state["last_signal"] = signal_type
            state["last_signal_window"] = window_key
            state["last_radar_sent_at"] = iso_now()
            state["last_radar_error"] = None
            logger.info("%s alert sent to Discord.", signal_type)
        else:
            state["last_radar_error"] = f"Discord webhook failed: {error}"
            logger.error("Discord webhook failed: %s", error)

    state["status"] = "running"
    save_state(state)
    return state


def stop_service(signum, frame) -> None:
    global RUNNING
    logger.info("Shutdown signal received.")
    RUNNING = False


def main() -> None:
    if not DISCORD_WEBHOOK_URL:
        raise SystemExit(
            "Missing DISCORD_WEBHOOK_URL.\n"
            "Set it first, for example:\n"
            'export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/ID/TOKEN"'
        )

    signal.signal(signal.SIGINT, stop_service)
    signal.signal(signal.SIGTERM, stop_service)

    logger.info("BTC Discord Radar started.")
    logger.info("Source: Coinbase BTC-USD | Timeframe: 15m")
    logger.info("Poll interval: %s seconds", POLL_SECONDS)

    state = load_state()

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
    logger.info("BTC Discord Radar stopped.")


if __name__ == "__main__":
    main()