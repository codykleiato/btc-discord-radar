#!/usr/bin/env python3
"""
btc_discord_radar.py

BTC 15-minute technical-analysis radar for Discord.
Informational only. No strategy can guarantee profits or win rate.

Install:
    pip install requests

Run:
    export DISCORD_WEBHOOK_URL="YOUR_DISCORD_WEBHOOK_URL"
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


# ── Settings ─────────────────────────────────────────────────────────────────

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

PAIR = "BTC-USD"
COINBASE_URL = f"https://api.coinbase.com/v2/prices/{PAIR}/spot"

POLL_SECONDS = 10
TIMEFRAME_MINUTES = 15

# Add/subtract this if the Kalshi target differs from the 15-minute opening price.
KALSHI_TARGET_OFFSET_USD = 0.0

# Trading levels. Adjust to your own risk rules.
ATR_LENGTH = 14
STOP_ATR_MULTIPLIER = 1.25
TAKE_PROFIT_ATR_MULTIPLIER = 1.75

FAST_EMA_LENGTH = 9
MID_EMA_LENGTH = 21
SLOW_EMA_LENGTH = 40
RSI_LENGTH = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Needs enough history to calculate EMA40, RSI14, MACD26, and ATR14.
MAX_PRICE_HISTORY = 250
MIN_HISTORY_FOR_ALERTS = 50

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


# ── Reliable HTTP requests ───────────────────────────────────────────────────

def build_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.5,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": "btc-discord-radar/2.0",
            "Accept": "application/json",
        }
    )
    return session


SESSION = build_session()


# ── State ────────────────────────────────────────────────────────────────────

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
        "prices": [],
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
        logger.warning("Could not read saved state: %s", error)
        return default_state()


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as error:
        logger.error("Could not save state: %s", error)


# ── Helpers ──────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def get_15m_window(now: datetime) -> datetime:
    rounded_minute = now.minute - (now.minute % TIMEFRAME_MINUTES)
    return now.replace(minute=rounded_minute, second=0, microsecond=0)


def money(value: Optional[float]) -> str:
    return "N/A" if value is None else f"${value:,.2f}"


def signed_money(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def get_btc_price() -> Optional[float]:
    try:
        response = SESSION.get(COINBASE_URL, timeout=(5, 45))
        response.raise_for_status()

        price = float(response.json()["data"]["amount"])
        if price <= 0:
            raise ValueError("Coinbase returned an invalid BTC price.")

        return price

    except (
        requests.exceptions.RequestException,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        logger.warning("Coinbase request failed: %s", error)
        return None


def ema(values: list[float], length: int) -> Optional[float]:
    if len(values) < length:
        return None

    smoothing = 2 / (length + 1)
    result = values[0]

    for value in values[1:]:
        result = (value - result) * smoothing + result

    return result


def rsi(values: list[float], length: int) -> Optional[float]:
    if len(values) < length + 1:
        return None

    changes = [
        values[index] - values[index - 1]
        for index in range(1, len(values))
    ]

    recent = changes[-length:]
    gains = [change for change in recent if change > 0]
    losses = [-change for change in recent if change < 0]

    average_gain = sum(gains) / length
    average_loss = sum(losses) / length

    if average_loss == 0:
        return 100.0

    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def macd(values: list[float]) -> tuple[Optional[float], Optional[float]]:
    if len(values) < MACD_SLOW + MACD_SIGNAL:
        return None, None

    macd_line_values = []

    for end in range(MACD_SLOW, len(values) + 1):
        subset = values[:end]
        fast = ema(subset, MACD_FAST)
        slow = ema(subset, MACD_SLOW)

        if fast is not None and slow is not None:
            macd_line_values.append(fast - slow)

    if len(macd_line_values) < MACD_SIGNAL:
        return None, None

    macd_line = macd_line_values[-1]
    signal_line = ema(macd_line_values, MACD_SIGNAL)

    return macd_line, signal_line


def atr(values: list[float], length: int) -> Optional[float]:
    if len(values) < length + 1:
        return None

    movements = [
        abs(values[index] - values[index - 1])
        for index in range(1, len(values))
    ]
    return sum(movements[-length:]) / length


# ── Analysis ─────────────────────────────────────────────────────────────────

def analyze(price: float, prices: list[float], target: float) -> Optional[dict]:
    if len(prices) < MIN_HISTORY_FOR_ALERTS:
        return None

    ema9 = ema(prices, FAST_EMA_LENGTH)
    ema21 = ema(prices, MID_EMA_LENGTH)
    ema40 = ema(prices, SLOW_EMA_LENGTH)
    rsi_value = rsi(prices, RSI_LENGTH)
    macd_line, signal_line = macd(prices)
    atr_value = atr(prices, ATR_LENGTH)

    if any(
        item is None
        for item in (
            ema9,
            ema21,
            ema40,
            rsi_value,
            macd_line,
            signal_line,
            atr_value,
        )
    ):
        return None

    bullish = 0
    bearish = 0
    reasons = []

    # 1. EMA9 vs EMA21
    if ema9 > ema21:
        bullish += 1
        reasons.append(("🟢", "EMA9 > EMA21"))
    else:
        bearish += 1
        reasons.append(("🔴", "EMA9 < EMA21"))

    # 2. BTC above/below EMA40
    if price > ema40:
        bullish += 1
        reasons.append(("🟢", "BTC above EMA40"))
    else:
        bearish += 1
        reasons.append(("🔴", "BTC below EMA40"))

    # 3. RSI
    if rsi_value >= 55:
        bullish += 1
        reasons.append(("🟢", f"RSI bullish ({rsi_value:.1f})"))
    elif rsi_value <= 45:
        bearish += 1
        reasons.append(("🔴", f"RSI bearish ({rsi_value:.1f})"))
    else:
        reasons.append(("⚪", f"RSI neutral ({rsi_value:.1f})"))

    # 4. MACD
    if macd_line > signal_line:
        bullish += 1
        reasons.append(("🟢", "MACD bullish"))
    else:
        bearish += 1
        reasons.append(("🔴", "MACD bearish"))

    # 5. Position relative to 15-minute target
    if price > target:
        bullish += 1
        reasons.append(("🟢", "BTC above 15m target"))
    else:
        bearish += 1
        reasons.append(("🔴", "BTC below 15m target"))

    gap = bullish - bearish

    if gap >= 2:
        signal_type = "UP"
    elif gap <= -2:
        signal_type = "DOWN"
    else:
        signal_type = "NEUTRAL"

    # Confidence is a score estimate, not a probability or win-rate claim.
    total_directional = bullish + bearish
    confidence = round((max(bullish, bearish) / total_directional) * 100)

    return {
        "signal_type": signal_type,
        "bullish": bullish,
        "bearish": bearish,
        "gap": gap,
        "confidence": confidence,
        "ema9": ema9,
        "ema21": ema21,
        "ema40": ema40,
        "rsi": rsi_value,
        "macd": macd_line,
        "macd_signal": signal_line,
        "atr": atr_value,
        "reasons": reasons,
    }


def trade_levels(price: float, signal_type: str, atr_value: float) -> tuple[float, float]:
    if signal_type == "UP":
        take_profit = price + (atr_value * TAKE_PROFIT_ATR_MULTIPLIER)
        stop_loss = price - (atr_value * STOP_ATR_MULTIPLIER)
    else:
        take_profit = price - (atr_value * TAKE_PROFIT_ATR_MULTIPLIER)
        stop_loss = price + (atr_value * STOP_ATR_MULTIPLIER)

    return take_profit, stop_loss


# ── Discord embed ────────────────────────────────────────────────────────────

def send_discord_callout(
    price: float,
    target: float,
    analysis: dict,
    window_start: datetime,
) -> tuple[bool, Optional[str]]:

    if not DISCORD_WEBHOOK_URL:
        return False, "DISCORD_WEBHOOK_URL is missing."

    signal_type = analysis["signal_type"]
    is_up = signal_type == "UP"

    if is_up:
        emoji = "🟢"
        title = "BTC UP SIGNAL"
        bet_text = "BET UP"
        color = 0x2ECC71
    else:
        emoji = "🔴"
        title = "BTC DOWN SIGNAL"
        bet_text = "BET DOWN"
        color = 0xE74C3C

    take_profit, stop_loss = trade_levels(
        price,
        signal_type,
        analysis["atr"],
    )

    analysis_text = "\n".join(
        f"{emoji_item} {reason}"
        for emoji_item, reason in analysis["reasons"]
    )

    hold_text = "2 candle(s) / up to 30 minutes"
    target_difference = price - target

    embed = {
        "title": f"{emoji} {title}",
        "description": (
            "**PKLA BTC 15-Minute Market Radar**\n"
            "Coinbase BTC-USD technical analysis"
        ),
        "color": color,
        "fields": [
            {
                "name": "₿ BTC PRICE",
                "value": f"**{money(price)}**",
                "inline": False,
            },
            {
                "name": "📊 SIGNAL",
                "value": f"**{bet_text}**",
                "inline": True,
            },
            {
                "name": "🎯 CONFIDENCE",
                "value": f"**{analysis['confidence']}%**",
                "inline": True,
            },
            {
                "name": "🕯️ HOLD",
                "value": f"**{hold_text}**",
                "inline": True,
            },
            {
                "name": "📈 SCORE",
                "value": (
                    f"Bullish: **{analysis['bullish']}**\n"
                    f"Bearish: **{analysis['bearish']}**\n"
                    f"Gap: **{analysis['gap']:+d}**"
                ),
                "inline": False,
            },
            {
                "name": "📐 INDICATORS",
                "value": (
                    f"RSI: **{analysis['rsi']:.1f}**\n"
                    f"MACD: **{analysis['macd']:.2f}**\n"
                    f"Signal: **{analysis['macd_signal']:.2f}**\n"
                    f"EMA9: **{money(analysis['ema9'])}**\n"
                    f"EMA21: **{money(analysis['ema21'])}**\n"
                    f"EMA40: **{money(analysis['ema40'])}**"
                ),
                "inline": False,
            },
            {
                "name": "📍 TRADE LEVELS",
                "value": (
                    f"Entry: **{money(price)}**\n"
                    f"Take Profit: **{money(take_profit)}**\n"
                    f"Stop Loss: **{money(stop_loss)}**"
                ),
                "inline": False,
            },
            {
                "name": "🎯 15-MINUTE TARGET",
                "value": (
                    f"Target: **{money(target)}**\n"
                    f"Difference: **{signed_money(target_difference)}**\n"
                    f"Window: **{window_start.strftime('%H:%M UTC')}**"
                ),
                "inline": False,
            },
            {
                "name": "🔬 ANALYSIS",
                "value": analysis_text,
                "inline": False,
            },
        ],
        "footer": {
            "text": (
                "Informational analysis only — not financial advice. "
                "No signal guarantees an outcome."
            )
        },
        "timestamp": iso_now(),
    }

    payload = {
        "username": "PKLA BTC Radar",
        "embeds": [embed],
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


# ── Scanner ──────────────────────────────────────────────────────────────────

def scan_once(state: dict) -> dict:
    now = utc_now()
    window_start = get_15m_window(now)
    window_key = window_start.isoformat()

    price = get_btc_price()

    if price is None:
        state["last_radar_error"] = "Coinbase BTC-USD request failed; retrying next scan."
        save_state(state)
        return state

    state["last_price"] = price
    state["last_radar_error"] = None

    if state.get("current_window_start") != window_key:
        state["current_window_start"] = window_key
        state["window_open_price"] = price
        state["kalshi_target"] = price + KALSHI_TARGET_OFFSET_USD
        state["last_signal"] = None
        state["last_signal_window"] = None

        logger.info(
            "New 15m window | Open=%s | Target=%s",
            money(price),
            money(state["kalshi_target"]),
        )

    prices = state.get("prices", [])
    prices.append(price)
    state["prices"] = prices[-MAX_PRICE_HISTORY:]

    analysis = analyze(
        price=price,
        prices=state["prices"],
        target=float(state["kalshi_target"]),
    )

    if analysis is None:
        logger.info(
            "Collecting price data: %s/%s samples.",
            len(state["prices"]),
            MIN_HISTORY_FOR_ALERTS,
        )
        save_state(state)
        return state

    signal_type = analysis["signal_type"]

    logger.info(
        "BTC=%s | Signal=%s | Bull=%s Bear=%s | Confidence=%s%%",
        money(price),
        signal_type,
        analysis["bullish"],
        analysis["bearish"],
        analysis["confidence"],
    )

    # Only UP or DOWN signals are sent. NEUTRAL produces no Discord message.
    already_sent = (
        state.get("last_signal") == signal_type
        and state.get("last_signal_window") == window_key
    )

    if signal_type in ("UP", "DOWN") and not already_sent:
        success, error = send_discord_callout(
            price=price,
            target=float(state["kalshi_target"]),
            analysis=analysis,
            window_start=window_start,
        )

        if success:
            state["last_signal"] = signal_type
            state["last_signal_window"] = window_key
            state["last_radar_sent_at"] = iso_now()
            logger.info("%s callout sent to Discord.", signal_type)
        else:
            state["last_radar_error"] = f"Discord webhook failed: {error}"
            logger.error("Discord webhook failed: %s", error)

    state["status"] = "running"
    save_state(state)
    return state


def stop_service(signum, frame) -> None:
    global RUNNING
    RUNNING = False
    logger.info("Stopping BTC radar...")


def main() -> None:
    if not DISCORD_WEBHOOK_URL:
        raise SystemExit(
            "DISCORD_WEBHOOK_URL is missing.\n"
            "Set it before running the script."
        )

    signal.signal(signal.SIGINT, stop_service)
    signal.signal(signal.SIGTERM, stop_service)

    state = load_state()

    logger.info("PKLA BTC 15-minute radar started.")
    logger.info("Polling Coinbase BTC-USD every %s seconds.", POLL_SECONDS)

    while RUNNING:
        try:
            state = scan_once(state)
        except Exception as error:
            state["last_radar_error"] = f"Unexpected error: {error}"
            state["status"] = "running"
            save_state(state)
            logger.exception("Unexpected scanner error")

        for _ in range(POLL_SECONDS):
            if not RUNNING:
                break
            time.sleep(1)

    state["status"] = "stopped"
    save_state(state)
    logger.info("BTC radar stopped.")


if __name__ == "__main__":
    main()