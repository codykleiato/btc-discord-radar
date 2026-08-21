import os
import time
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

# ============================================================
# CONFIG
# ============================================================

PORT = int(os.environ.get("PORT", "10000"))

COINBASE_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
GRANULARITY = 900  # 15 minutes

CHECK_INTERVAL = 10

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# Send a Discord message for every closed 15-minute candle.
SEND_EVERY_15M = True

# Retry settings
MAX_DISCORD_ATTEMPTS = 5
DEFAULT_RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 300

# Prevent accidental repeated /test requests.
TEST_COOLDOWN = 60
last_test_time = 0.0

# Prevent processing the same candle twice.
last_processed_candle = None

app = Flask(__name__)


# ============================================================
# DISPLAY
# ============================================================

def print_banner():
    print()
    print("=" * 50)
    print("       PKLA BTC DISCORD RADAR")
    print("=" * 50)
    print("Data: Coinbase Exchange")
    print("Market: BTC-USD")
    print("Timeframe: 15M")
    print("TradingView webhook: NOT REQUIRED")
    print("Discord: WEBHOOK")
    print("SEND EVERY 15M: ENABLED")
    print(f"Check interval: {CHECK_INTERVAL}s")
    print("Discord retry/backoff: ENABLED")
    print(f"Discord max attempts: {MAX_DISCORD_ATTEMPTS}")
    print("=" * 50)

    if DISCORD_WEBHOOK_URL:
        print("Discord webhook: CONFIGURED")
    else:
        print("WARNING: DISCORD_WEBHOOK_URL is missing.")

    print()


# ============================================================
# COINBASE
# ============================================================

def get_candles():
    print("Requesting BTC candles from Coinbase...")

    params = {
        "granularity": GRANULARITY
    }

    response = requests.get(
        COINBASE_URL,
        params=params,
        timeout=20,
        headers={
            "User-Agent": "PKLA-BTC-Discord-Radar/1.0"
        }
    )

    response.raise_for_status()

    candles = response.json()

    if not isinstance(candles, list):
        raise ValueError("Coinbase returned an unexpected response.")

    print(f"Coinbase returned {len(candles)} candles.")

    return candles


def get_latest_closed_candle(candles):
    if not candles:
        raise ValueError("No candles returned by Coinbase.")

    now = int(time.time())

    closed = []

    for candle in candles:
        if len(candle) < 6:
            continue

        timestamp = int(candle[0])

        if timestamp + GRANULARITY <= now:
            closed.append(candle)

    if not closed:
        return None

    return max(closed, key=lambda x: int(x[0]))


# ============================================================
# ANALYSIS
# ============================================================

def analyze_btc(candles, latest_candle):
    """
    Simple deterministic BTC signal calculation.

    Candle format:
    [time, low, high, open, close, volume]
    """

    if len(candles) < 30:
        raise ValueError("Not enough candles for analysis.")

    ordered = sorted(candles, key=lambda x: int(x[0]))

    closes = [float(c[4]) for c in ordered]

    current_price = float(latest_candle[4])

    # Recent closes
    recent = closes[-20:]

    sma_5 = sum(recent[-5:]) / 5
    sma_10 = sum(recent[-10:]) / 10
    sma_20 = sum(recent[-20:]) / 20

    bullish_score = 0
    bearish_score = 0

    # Price vs moving averages
    if current_price > sma_5:
        bullish_score += 2
    else:
        bearish_score += 2

    if current_price > sma_10:
        bullish_score += 2
    else:
        bearish_score += 2

    if current_price > sma_20:
        bullish_score += 2
    else:
        bearish_score += 2

    # Short-term momentum
    if closes[-1] > closes[-2]:
        bullish_score += 2
    else:
        bearish_score += 2

    if closes[-1] > closes[-3]:
        bullish_score += 2
    else:
        bearish_score += 2

    # Candle direction
    candle_open = float(latest_candle[3])
    candle_close = float(latest_candle[4])

    if candle_close > candle_open:
        bullish_score += 2
    elif candle_close < candle_open:
        bearish_score += 2

    # Additional momentum
    if closes[-5] < closes[-1]:
        bullish_score += 2
    elif closes[-5] > closes[-1]:
        bearish_score += 2

    total = bullish_score + bearish_score

    if bullish_score >= 8 and bullish_score > bearish_score:
        signal = "⬆️ BET UP"
        confidence = min(
            95,
            max(
                55,
                50 + int(
                    (bullish_score - bearish_score) / max(total, 1) * 50
                )
            )
        )
    elif bearish_score >= 8 and bearish_score > bullish_score:
        signal = "⬇️ BET DOWN"
        confidence = min(
            95,
            max(
                55,
                50 + int(
                    (bearish_score - bullish_score) / max(total, 1) * 50
                )
            )
        )
    else:
        signal = "⏸️ NO TRADE"
        confidence = 50

    return {
        "price": current_price,
        "signal": signal,
        "confidence": confidence,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "candle_time": int(latest_candle[0])
    }


# ============================================================
# DISCORD WEBHOOK
# ============================================================

def build_discord_message(result, test=False):
    candle_time = datetime.fromtimestamp(
        result["candle_time"],
        tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

    if test:
        return (
            "🧪 **PKLA BTC DISCORD RADAR TEST**\n\n"
            "Discord webhook connection is working.\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        )

    return (
        "🚨 **PKLA BTC DISCORD RADAR** 🚨\n\n"
        f"**BTC-USD:** ${result['price']:,.2f}\n"
        f"**Signal:** {result['signal']}\n"
        f"**Confidence:** {result['confidence']}%\n"
        f"**Bullish Score:** {result['bullish_score']}\n"
        f"**Bearish Score:** {result['bearish_score']}\n"
        f"**15M Candle:** {candle_time}\n\n"
        "⏱️ **Next signal evaluation: next closed 15M candle**"
    )


def send_to_discord(message):
    if not DISCORD_WEBHOOK_URL:
        print("Discord webhook is missing.")
        return False

    payload = {
        "content": message,
        "allowed_mentions": {
            "parse": []
        }
    }

    retry_seconds = DEFAULT_RETRY_SECONDS

    for attempt in range(1, MAX_DISCORD_ATTEMPTS + 1):
        print(
            f"Discord webhook attempt "
            f"{attempt}/{MAX_DISCORD_ATTEMPTS}..."
        )

        try:
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                json=payload,
                timeout=20,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "PKLA-BTC-Discord-Radar/1.0"
                }
            )

            # Discord webhook success.
            if response.status_code in (200, 204):
                print("✅ Discord message sent successfully.")
                return True

            # Rate limited.
            if response.status_code == 429:
                retry_after = None

                try:
                    data = response.json()
                    retry_after = data.get("retry_after")
                except Exception:
                    pass

                if retry_after is None:
                    retry_after = response.headers.get("Retry-After")

                try:
                    retry_after = float(retry_after)
                except (TypeError, ValueError):
                    retry_after = retry_seconds

                retry_after = max(1, retry_after)

                print(
                    f"Discord returned 429 rate limit. "
                    f"Waiting {retry_after:.1f}s..."
                )

                if attempt < MAX_DISCORD_ATTEMPTS:
                    time.sleep(retry_after)

                    retry_seconds = min(
                        retry_seconds * 2,
                        MAX_RETRY_SECONDS
                    )

                    continue

                print("Discord rate limit persisted.")
                return False

            # Server errors: retry.
            if 500 <= response.status_code <= 599:
                print(
                    f"Discord server error "
                    f"{response.status_code}."
                )

                if attempt < MAX_DISCORD_ATTEMPTS:
                    print(
                        f"Waiting {retry_seconds}s before retry..."
                    )

                    time.sleep(retry_seconds)

                    retry_seconds = min(
                        retry_seconds * 2,
                        MAX_RETRY_SECONDS
                    )

                    continue

                return False

            # Other HTTP error.
            print(
                f"Discord rejected message: "
                f"HTTP {response.status_code}"
            )
            print(response.text[:1000])

            return False

        except requests.RequestException as exc:
            print(f"Discord connection error: {exc}")

            if attempt < MAX_DISCORD_ATTEMPTS:
                print(
                    f"Waiting {retry_seconds}s before retry..."
                )

                time.sleep(retry_seconds)

                retry_seconds = min(
                    retry_seconds * 2,
                    MAX_RETRY_SECONDS
                )

                continue

            return False

    return False


# ============================================================
# PROCESS CANDLE
# ============================================================

def process_latest_candle():
    global last_processed_candle

    print("Checking Coinbase for new closed candle...")

    candles = get_candles()

    latest = get_latest_closed_candle(candles)

    if latest is None:
        print("No closed candle available yet.")
        return

    candle_timestamp = int(latest[0])

    candle_iso = datetime.fromtimestamp(
        candle_timestamp,
        tz=timezone.utc
    ).isoformat()

    print(f"Latest closed candle: {candle_iso}")

    if last_processed_candle == candle_timestamp:
        print("No new closed 15M candle yet.")
        return

    print("New closed 15M candle detected!")
    print("Analyzing BTC...")

    # Fetch fresh candles for analysis.
    candles = get_candles()

    result = analyze_btc(candles, latest)

    print(f"Price: ${result['price']:,.2f}")
    print(f"Signal: {result['signal']}")
    print(f"Confidence: {result['confidence']}%")
    print(f"Bullish score: {result['bullish_score']}")
    print(f"Bearish score: {result['bearish_score']}")
    print(f"Candle analyzed: {candle_iso}")

    # Mark candle processed regardless of signal.
    last_processed_candle = candle_timestamp

    # IMPORTANT:
    # NO TRADE does not send a Discord alert.
    if result["signal"] == "⏸️ NO TRADE":
        print("NO TRADE signal - Discord message skipped.")
        print("Candle processing complete.")
        return

    print("Sending 15-minute result to Discord...")

    message = build_discord_message(result)

    success = send_to_discord(message)

    if success:
        print("15-minute Discord signal sent successfully.")
    else:
        print("Signal was NOT sent to Discord.")

    print("Candle processing complete.")


# ============================================================
# RADAR LOOP
# ============================================================

def radar_loop():
    print("Radar thread started.")

    while True:
        try:
            process_latest_candle()

        except Exception as exc:
            print(f"Radar error: {exc}")

        time.sleep(CHECK_INTERVAL)


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "service": "PKLA BTC Discord Radar",
        "market": "BTC-USD",
        "timeframe": "15M",
        "send_every_15m": True,
        "discord_webhook_configured": bool(DISCORD_WEBHOOK_URL)
    })


@app.route("/test")
def test_discord():
    global last_test_time

    now = time.time()

    elapsed = now - last_test_time

    if elapsed < TEST_COOLDOWN:
        remaining = int(TEST_COOLDOWN - elapsed) + 1

        return jsonify({
            "status": "rate_limited_locally",
            "message": "Test endpoint is cooling down.",
            "retry_after": remaining
        }), 429

    last_test_time = now

    print("Manual Discord test requested.")

    message = build_discord_message(
        {
            "candle_time": int(time.time()),
            "price": 0,
            "signal": "TEST",
            "confidence": 0,
            "bullish_score": 0,
            "bearish_score": 0
        },
        test=True
    )

    success = send_to_discord(message)

    if success:
        return jsonify({
            "status": "success",
            "message": "Test message sent to Discord."
        }), 200

    return jsonify({
        "status": "error",
        "message": "Discord webhook delivery failed. Check Render logs."
    }), 502


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    print("Starting PKLA BTC Discord Radar...")
    print_banner()

    radar_thread = threading.Thread(
        target=radar_loop,
        daemon=True
    )

    radar_thread.start()

    print(f"Starting web server on port {PORT}...")

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        threaded=True
    )