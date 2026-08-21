import os
import time
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

PORT = int(os.environ.get("PORT", "10000"))

COINBASE_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
GRANULARITY = 900
CHECK_INTERVAL = 10

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

SEND_EVERY_15M = True

MAX_DISCORD_ATTEMPTS = 5
DEFAULT_RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 300

last_processed_candle = None

app = Flask(**name**)

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
print("Manual /test cooldown: DISABLED")
print("=" * 50)

```
if DISCORD_WEBHOOK_URL:
    print("Discord webhook: CONFIGURED")
else:
    print("WARNING: DISCORD_WEBHOOK_URL is missing.")

print()
```

def get_candles():
print("Requesting BTC candles from Coinbase...")

```
response = requests.get(
    COINBASE_URL,
    params={"granularity": GRANULARITY},
    timeout=20,
    headers={
        "User-Agent": "PKLA-BTC-Discord-Radar/1.0"
    }
)

response.raise_for_status()

candles = response.json()

if not isinstance(candles, list):
    raise ValueError(
        "Coinbase returned an unexpected response."
    )

print(f"Coinbase returned {len(candles)} candles.")

return candles
```

def get_latest_closed_candle(candles):
if not candles:
raise ValueError(
"No candles returned by Coinbase."
)

```
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

return max(
    closed,
    key=lambda x: int(x[0])
)
```

def analyze_btc(candles, latest_candle):
if len(candles) < 30:
raise ValueError(
"Not enough candles for analysis."
)

```
ordered = sorted(
    candles,
    key=lambda x: int(x[0])
)

closes = [
    float(candle[4])
    for candle in ordered
]

current_price = float(
    latest_candle[4]
)

recent = closes[-20:]

sma_5 = sum(recent[-5:]) / 5
sma_10 = sum(recent[-10:]) / 10
sma_20 = sum(recent[-20:]) / 20

bullish_score = 0
bearish_score = 0

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

if closes[-1] > closes[-2]:
    bullish_score += 2
else:
    bearish_score += 2

if closes[-1] > closes[-3]:
    bullish_score += 2
else:
    bearish_score += 2

candle_open = float(
    latest_candle[3]
)

candle_close = float(
    latest_candle[4]
)

if candle_close > candle_open:
    bullish_score += 2

elif candle_close < candle_open:
    bearish_score += 2

if closes[-5] < closes[-1]:
    bullish_score += 2

elif closes[-5] > closes[-1]:
    bearish_score += 2

total = bullish_score + bearish_score

if (
    bullish_score >= 8
    and bullish_score > bearish_score
):
    signal = "⬆️ BET UP"

    confidence = min(
        95,
        max(
            55,
            50 + int(
                (
                    bullish_score
                    - bearish_score
                )
                / max(total, 1)
                * 50
            )
        )
    )

elif (
    bearish_score >= 8
    and bearish_score > bullish_score
):
    signal = "⬇️ BET DOWN"

    confidence = min(
        95,
        max(
            55,
            50 + int(
                (
                    bearish_score
                    - bullish_score
                )
                / max(total, 1)
                * 50
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
    "candle_time": int(
        latest_candle[0]
    )
}
```

def build_discord_message(
result,
test=False
):
if test:
current_time = datetime.now(
timezone.utc
).strftime(
"%Y-%m-%d %H:%M:%S UTC"
)

```
    return (
        "🧪 **PKLA BTC DISCORD RADAR TEST**\n\n"
        "✅ Discord webhook connection is working.\n"
        f"Time: {current_time}"
    )

candle_time = datetime.fromtimestamp(
    result["candle_time"],
    tz=timezone.utc
).strftime(
    "%Y-%m-%d %H:%M:%S UTC"
)

return (
    "🚨 **PKLA BTC DISCORD RADAR** 🚨\n\n"
    f"**BTC-USD:** "
    f"${result['price']:,.2f}\n"
    f"**Signal:** "
    f"{result['signal']}\n"
    f"**Confidence:** "
    f"{result['confidence']}%\n"
    f"**Bullish Score:** "
    f"{result['bullish_score']}\n"
    f"**Bearish Score:** "
    f"{result['bearish_score']}\n"
    f"**15M Candle:** "
    f"{candle_time}\n\n"
    "⏱️ **Next signal evaluation: "
    "next closed 15M candle**"
)
```

def send_to_discord(message):
if not DISCORD_WEBHOOK_URL:
print(
"Discord webhook is missing."
)
return False

```
payload = {
    "content": message,
    "allowed_mentions": {
        "parse": []
    }
}

retry_seconds = (
    DEFAULT_RETRY_SECONDS
)

for attempt in range(
    1,
    MAX_DISCORD_ATTEMPTS + 1
):
    print(
        f"Discord webhook attempt "
        f"{attempt}/"
        f"{MAX_DISCORD_ATTEMPTS}..."
    )

    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=20,
            headers={
                "Content-Type":
                    "application/json",
                "User-Agent":
                    "PKLA-BTC-Discord-Radar/1.0"
            }
        )

        if response.status_code in (
            200,
            204
        ):
            print(
                "Discord message sent successfully."
            )
            return True

        if response.status_code == 429:
            retry_after = None

            try:
                data = response.json()
                retry_after = data.get(
                    "retry_after"
                )
            except Exception:
                pass

            if retry_after is None:
                retry_after = (
                    response.headers.get(
                        "Retry-After"
                    )
                )

            try:
                retry_after = float(
                    retry_after
                )
            except (
                TypeError,
                ValueError
            ):
                retry_after = (
                    retry_seconds
                )

            retry_after = max(
                1,
                retry_after
            )

            print(
                "Discord returned 429 "
                "rate limit. "
                f"Waiting "
                f"{retry_after:.1f}s..."
            )

            if attempt < (
                MAX_DISCORD_ATTEMPTS
            ):
                time.sleep(
                    retry_after
                )

                retry_seconds = min(
                    retry_seconds * 2,
                    MAX_RETRY_SECONDS
                )

                continue

            return False

        if (
            500
            <= response.status_code
            <= 599
        ):
            print(
                "Discord server error "
                f"{response.status_code}."
            )

            if attempt < (
                MAX_DISCORD_ATTEMPTS
            ):
                print(
                    "Waiting "
                    f"{retry_seconds}s "
                    "before retry..."
                )

                time.sleep(
                    retry_seconds
                )

                retry_seconds = min(
                    retry_seconds * 2,
                    MAX_RETRY_SECONDS
                )

                continue

            return False

        print(
            "Discord rejected message: "
            f"HTTP "
            f"{response.status_code}"
        )

        print(
            response.text[:1000]
        )

        return False

    except requests.RequestException as exc:
        print(
            f"Discord connection error: "
            f"{exc}"
        )

        if attempt < (
            MAX_DISCORD_ATTEMPTS
        ):
            print(
                "Waiting "
                f"{retry_seconds}s "
                "before retry..."
            )

            time.sleep(
                retry_seconds
            )

            retry_seconds = min(
                retry_seconds * 2,
                MAX_RETRY_SECONDS
            )

            continue

        return False

return False
```

def process_latest_candle():
global last_processed_candle

```
print(
    "Checking Coinbase for "
    "new closed candle..."
)

candles = get_candles()

latest = get_latest_closed_candle(
    candles
)

if latest is None:
    print(
        "No closed candle available yet."
    )
    return

candle_timestamp = int(
    latest[0]
)

candle_time = datetime.fromtimestamp(
    candle_timestamp,
    tz=timezone.utc
).isoformat()

print(
    f"Latest closed candle: "
    f"{candle_time}"
)

if (
    last_processed_candle
    == candle_timestamp
):
    print(
        "No new closed 15M candle yet."
    )
    return

print(
    "New closed 15M candle detected!"
)

print("Analyzing BTC...")

candles = get_candles()

latest = get_latest_closed_candle(
    candles
)

if latest is None:
    print(
        "No closed candle available."
    )
    return

result = analyze_btc(
    candles,
    latest
)

print(
    f"Price: "
    f"${result['price']:,.2f}"
)

print(
    f"Signal: "
    f"{result['signal']}"
)

print(
    f"Confidence: "
    f"{result['confidence']}%"
)

print(
    f"Bullish score: "
    f"{result['bullish_score']}"
)

print(
    f"Bearish score: "
    f"{result['bearish_score']}"
)

last_processed_candle = int(
    latest[0]
)

if (
    result["signal"]
    == "⏸️ NO TRADE"
):
    print(
        "NO TRADE signal - "
        "Discord message skipped."
    )
    return

print(
    "Sending 15-minute result "
    "to Discord..."
)

message = build_discord_message(
    result
)

success = send_to_discord(
    message
)

if success:
    print(
        "15-minute Discord signal "
        "sent successfully."
    )
else:
    print(
        "Signal was NOT sent "
        "to Discord."
    )
```

def radar_loop():
print(
"Radar thread started."
)

```
while True:
    try:
        process_latest_candle()

    except Exception as exc:
        print(
            f"Radar error: {exc}"
        )

    time.sleep(
        CHECK_INTERVAL
    )
```

@app.route("/")
def home():
return jsonify({
"status": "online",
"service":
"PKLA BTC Discord Radar",
"market": "BTC-USD",
"timeframe": "15M",
"send_every_15m":
SEND_EVERY_15M,
"discord_webhook_configured":
bool(DISCORD_WEBHOOK_URL),
"test_endpoint":
"/test",
"health_endpoint":
"/health"
})

@app.route("/health")
def health():
return jsonify({
"status": "healthy",
"service":
"PKLA BTC Discord Radar"
}), 200

@app.route("/test")
def test_discord():
print(
"Manual Discord test requested."
)

```
if not DISCORD_WEBHOOK_URL:
    return jsonify({
        "status": "error",
        "message":
            "DISCORD_WEBHOOK_URL "
            "is not configured."
    }), 500

message = build_discord_message(
    {
        "candle_time":
            int(time.time()),
        "price": 0,
        "signal": "TEST",
        "confidence": 0,
        "bullish_score": 0,
        "bearish_score": 0
    },
    test=True
)

def send_test():
    print(
        "Sending Discord test..."
    )

    success = send_to_discord(
        message
    )

    if success:
        print(
            "Discord test sent successfully."
        )
    else:
        print(
            "Discord test failed."
        )

thread = threading.Thread(
    target=send_test,
    daemon=True
)

thread.start()

return jsonify({
    "status": "accepted",
    "message":
        "Discord test started. "
        "Check your Discord channel.",
    "discord_webhook_configured":
        True
}), 202
```

if **name** == "**main**":
print(
"Starting PKLA BTC Discord Radar..."
)

```
print_banner()

radar_thread = threading.Thread(
    target=radar_loop,
    daemon=True
)

radar_thread.start()

print(
    f"Starting web server "
    f"on port {PORT}..."
)

app.run(
    host="0.0.0.0",
    port=PORT,
    debug=False,
    threaded=True
)
```
