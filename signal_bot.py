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

logger = logging.getLogger("btc-discord-radar")


# ── Reliable HTTP requests ───────────────────────────────────────────────────

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
            "User-Agent": "btc-discord-radar/3.0",
            "Accept": "application/json",
        }
    )
    return session


SESSION = build_session()


# ── State ────────────────────────────────────────────────────────────────────

def default_state() -> dict:
    return {
        "service": "btc-discord-radar",
        "source": "Coinbase, Kraken, Bitstamp, CF Benchmarks",
        "status": "running",
        "timeframe": "15m",
        "current_window_start": None,
        "last_open_alert_window": None,
        "last_close_alert_window": None,
        "last_radar_error": None,
        "last_radar_sent_at": None,
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
        logger.warning("Could not load state: %s", error)
        return default_state()


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as error:
        logger.error("Could not save state: %s", error)


# ── Time helpers ─────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def candle_window(now: datetime) -> datetime:
    minute = now.minute - (now.minute % TIMEFRAME_MINUTES)
    return now.replace(minute=minute, second=0, microsecond=0)


def money(value: Optional[float]) -> str:
    return "N/A" if value is None else f"${value:,.2f}"


def percent(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:+.3f}%"


# ── Market sources ───────────────────────────────────────────────────────────
# Each function returns:
# {"name": str, "price": float, "open": float, "high": float, "low": float, "close": float}

def get_coinbase_15m() -> Optional[dict]:
    """
    Coinbase Advanced Trade public candles:
    BTC-USD, 15-minute bars.
    """
    try:
        url = "https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD/candles"
        params = {
            "start": str(int(time.time()) - 3600),
            "end": str(int(time.time())),
            "granularity": "FIFTEEN_MINUTE",
            "limit": 5,
        }

        response = SESSION.get(url, params=params, timeout=(5, 30))
        response.raise_for_status()

        candles = response.json().get("candles", [])
        if not candles:
            raise ValueError("Coinbase returned no candles.")

        latest = max(candles, key=lambda item: int(item["start"]))

        return {
            "name": "Coinbase",
            "price": float(latest["close"]),
            "open": float(latest["open"]),
            "high": float(latest["high"]),
            "low": float(latest["low"]),
            "close": float(latest["close"]),
        }

    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        logger.warning("Coinbase failed: %s", error)
        return None


def get_kraken_15m() -> Optional[dict]:
    """
    Kraken public OHLC endpoint.
    The final OHLC row is Kraken's currently active 15-minute candle.
    """
    try:
        url = "https://api.kraken.com/0/public/OHLC"
        response = SESSION.get(
            url,
            params={"pair": "XBTUSD", "interval": 15},
            timeout=(5, 30),
        )
        response.raise_for_status()

        payload = response.json()

        if payload.get("error"):
            raise ValueError(", ".join(payload["error"]))

        result = payload["result"]
        pair_key = next(key for key in result if key != "last")
        latest = result[pair_key][-1]

        return {
            "name": "Kraken",
            "price": float(latest[4]),
            "open": float(latest[1]),
            "high": float(latest[2]),
            "low": float(latest[3]),
            "close": float(latest[4]),
        }

    except (requests.RequestException, KeyError, TypeError, ValueError, StopIteration) as error:
        logger.warning("Kraken failed: %s", error)
        return None


def get_bitstamp_15m() -> Optional[dict]:
    """
    Bitstamp public OHLC endpoint.
    step=900 seconds = 15 minutes.
    """
    try:
        url = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
        response = SESSION.get(
            url,
            params={"step": 900, "limit": 3},
            timeout=(5, 30),
        )
        response.raise_for_status()

        candles = response.json()["data"]["ohlc"]
        latest = max(candles, key=lambda item: int(item["timestamp"]))

        return {
            "name": "Bitstamp",
            "price": float(latest["close"]),
            "open": float(latest["open"]),
            "high": float(latest["high"]),
            "low": float(latest["low"]),
            "close": float(latest["close"]),
        }

    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        logger.warning("Bitstamp failed: %s", error)
        return None


def get_cf_benchmark() -> Optional[dict]:
    """
    CF Benchmarks reference-rate endpoint availability can vary by product/access.
    This attempts the public BRR reference endpoint and safely skips it if unavailable.

    If you have a licensed CF Benchmarks API endpoint, replace CF_URL below
    with your assigned endpoint and provide its API key as an environment variable.
    """
    try:
        cf_url = os.getenv(
            "CF_BENCHMARKS_URL",
            "https://www.cfbenchmarks.com/data/indices/BRR",
        )

        response = SESSION.get(cf_url, timeout=(5, 30))
        response.raise_for_status()

        payload = response.json()

        # Supports common JSON structures if a valid CF data endpoint is provided.
        raw_price = (
            payload.get("price")
            or payload.get("value")
            or payload.get("level")
            or payload.get("data", {}).get("price")
            or payload.get("data", {}).get("value")
        )

        if raw_price is None:
            raise ValueError("No usable CF Benchmarks price field found.")

        price = float(raw_price)

        return {
            "name": "CF Benchmarks",
            "price": price,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
        }

    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        logger.info("CF Benchmarks skipped: %s", error)
        return None


def get_all_markets() -> list[dict]:
    markets = [
        get_coinbase_15m(),
        get_kraken_15m(),
        get_bitstamp_15m(),
        get_cf_benchmark(),
    ]
    return [market for market in markets if market is not None]


# ── Signal calculation ───────────────────────────────────────────────────────

def market_direction(market: dict) -> tuple[str, float]:
    change_pct = ((market["close"] - market["open"]) / market["open"]) * 100

    if market["close"] > market["open"]:
        return "UP", change_pct

    if market["close"] < market["open"]:
        return "DOWN", change_pct

    return "FLAT", change_pct


def consensus(markets: list[dict]) -> tuple[str, int, int]:
    up_count = 0
    down_count = 0

    for market in markets:
        direction, _ = market_direction(market)

        if direction == "UP":
            up_count += 1
        elif direction == "DOWN":
            down_count += 1

    if up_count > down_count:
        return "UP", up_count, down_count

    if down_count > up_count:
        return "DOWN", up_count, down_count

    return "MIXED", up_count, down_count


# ── Discord ──────────────────────────────────────────────────────────────────

def send_discord_candle_alert(
    event: str,
    window_start: datetime,
    markets: list[dict],
) -> tuple[bool, Optional[str]]:

    if not DISCORD_WEBHOOK_URL:
        return False, "DISCORD_WEBHOOK_URL is missing."

    direction, up_count, down_count = consensus(markets)

    if direction == "UP":
        emoji = "🟢"
        color = 0x2ECC71
        signal_text = "BET UP"
    elif direction == "DOWN":
        emoji = "🔴"
        color = 0xE74C3C
        signal_text = "BET DOWN"
    else:
        emoji = "⚪"
        color = 0x95A5A6
        signal_text = "NO CONSENSUS"

    event_text = "NEW 15-MINUTE CANDLE OPENED" if event == "OPEN" else "15-MINUTE CANDLE CLOSED"

    market_lines = []

    for market in markets:
        market_signal, change_pct = market_direction(market)

        symbol = (
            "🟢" if market_signal == "UP"
            else "🔴" if market_signal == "DOWN"
            else "⚪"
        )

        market_lines.append(
            f"{symbol} **{market['name']}**\n"
            f"Open: {money(market['open'])}\n"
            f"Close: {money(market['close'])}\n"
            f"Change: **{percent(change_pct)}**"
        )

    fields = [
        {
            "name": "📊 CONSENSUS",
            "value": (
                f"**{signal_text}**\n"
                f"Up: **{up_count}** | Down: **{down_count}**"
            ),
            "inline": False,
        },
        {
            "name": "🕒 15-MINUTE WINDOW",
            "value": (
                f"Start: **{window_start.strftime('%Y-%m-%d %H:%M UTC')}**\n"
                f"Event: **{event_text}**"
            ),
            "inline": False,
        },
    ]

    for market_line in market_lines:
        market_name = market_line.split("**")[1]
        fields.append(
            {
                "name": f"₿ {market_name}",
                "value": market_line,
                "inline": True,
            }
        )

    payload = {
        "username": "PKLA BTC Radar",
        "embeds": [
            {
                "title": f"{emoji} BTC {event_text}",
                "description": (
                    "**15-minute BTC market scan**\n"
                    "Coinbase, Kraken, Bitstamp, and CF Benchmarks when available."
                ),
                "color": color,
                "fields": fields,
                "footer": {
                    "text": (
                        "Informational market data only — not financial advice. "
                        "Signals do not guarantee outcomes."
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


# ── Main scanner ─────────────────────────────────────────────────────────────

def scan_once(state: dict) -> dict:
    now = utc_now()
    current_window = candle_window(now)
    current_key = current_window.isoformat()

    previous_window = current_window.replace(
        minute=current_window.minute - TIMEFRAME_MINUTES
    )

    # Handle the hour rollover safely.
    if current_window.minute < TIMEFRAME_MINUTES:
        previous_window = datetime.fromtimestamp(
            current_window.timestamp() - (TIMEFRAME_MINUTES * 60),
            tz=timezone.utc,
        )

    markets = get_all_markets()

    if not markets:
        state["last_radar_error"] = "All market requests failed; retrying next scan."
        save_state(state)
        return state

    state["last_radar_error"] = None
    state["status"] = "running"

    # Send close message first when a new 15m window begins.
    # The data collected immediately before this point represents the closed candle.
    old_window_key = previous_window.isoformat()

    if (
        state.get("current_window_start") is not None
        and state.get("last_close_alert_window") != old_window_key
        and state.get("current_window_start") != current_key
    ):
        success, error = send_discord_candle_alert(
            event="CLOSE",
            window_start=previous_window,
            markets=markets,
        )

        if success:
            state["last_close_alert_window"] = old_window_key
            state["last_radar_sent_at"] = iso_now()
            logger.info("15m candle CLOSE alert sent.")
        else:
            state["last_radar_error"] = f"Discord close alert failed: {error}"

    # Send new candle message once for the current window.
    if state.get("last_open_alert_window") != current_key:
        success, error = send_discord_candle_alert(
            event="OPEN",
            window_start=current_window,
            markets=markets,
        )

        if success:
            state["last_open_alert_window"] = current_key
            state["last_radar_sent_at"] = iso_now()
            logger.info("15m candle OPEN alert sent.")
        else:
            state["last_radar_error"] = f"Discord open alert failed: {error}"

    state["current_window_start"] = current_key
    save_state(state)
    return state


def shutdown(signum, frame) -> None:
    global RUNNING
    RUNNING = False
    logger.info("Shutdown signal received.")


def main() -> None:
    if not DISCORD_WEBHOOK_URL:
        raise SystemExit("DISCORD_WEBHOOK_URL is missing in Render Environment.")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    state = load_state()

    logger.info("BTC multi-market radar started.")
    logger.info("Sources: Coinbase, Kraken, Bitstamp, CF Benchmarks.")
    logger.info("Sending alerts when every 15m candle opens and closes.")

    while RUNNING:
        try:
            state = scan_once(state)
        except Exception as error:
            state["last_radar_error"] = f"Unexpected scanner error: {error}"
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