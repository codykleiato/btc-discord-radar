import os
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()


@app.get("/")
def home():
    return jsonify(
        {
            "service": "btc-discord-radar",
            "status": "running",
            "mode": "tradingview-to-discord",
            "webhook_endpoint": "/webhook",
        }
    )


@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


def send_discord_message(message):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not configured.")

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={
            "content": message,
            "allowed_mentions": {"parse": []},
        },
        timeout=20,
    )
    response.raise_for_status()


def clean_value(value, fallback="N/A"):
    if value is None:
        return fallback

    value = str(value).strip()
    return value if value else fallback


def signal_emoji(signal):
    signal = signal.upper()

    if signal in {"BUY", "LONG", "UP"}:
        return "🟢"

    if signal in {"SELL", "SHORT", "DOWN"}:
        return "🔴"

    if signal in {"EXIT", "CLOSE", "FLAT"}:
        return "🟡"

    return "🔵"


@app.post("/webhook")
def tradingview_webhook():
    if not request.is_json:
        return jsonify(
            {
                "ok": False,
                "error": "Expected an application/json request body.",
            }
        ), 400

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify(
            {
                "ok": False,
                "error": "Invalid JSON object.",
            }
        ), 400

    supplied_secret = clean_value(payload.get("secret"), "")
    if not WEBHOOK_SECRET or supplied_secret != WEBHOOK_SECRET:
        return jsonify(
            {
                "ok": False,
                "error": "Unauthorized webhook.",
            }
        ), 401

    signal = clean_value(
        payload.get("signal")
        or payload.get("action")
        or payload.get("side")
        or payload.get("direction"),
        "SIGNAL",
    ).upper()

    ticker = clean_value(
        payload.get("ticker")
        or payload.get("symbol")
        or payload.get("instrument"),
        "BTCUSD",
    )

    price = clean_value(
        payload.get("price")
        or payload.get("close")
        or payload.get("market_price"),
    )

    timeframe = clean_value(
        payload.get("timeframe")
        or payload.get("interval"),
    )

    confidence = clean_value(
        payload.get("confidence")
        or payload.get("score")
        or payload.get("probability"),
        "",
    )

    reason = clean_value(
        payload.get("reason")
        or payload.get("message")
        or payload.get("strategy"),
        "",
    )

    event_time = clean_value(
        payload.get("time")
        or payload.get("timestamp")
        or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    emoji = signal_emoji(signal)

    lines = [
        f"{emoji} **{signal} SIGNAL**",
        f"**Ticker:** `{ticker}`",
        f"**Price:** `{price}`",
        f"**Timeframe:** `{timeframe}`",
        f"**Time:** `{event_time}`",
    ]

    if confidence:
        lines.append(f"**Confidence:** `{confidence}`")

    if reason:
        lines.append(f"**Details:** {reason}")

    try:
        send_discord_message("\n".join(lines))
        print(f"Discord signal sent: {signal} {ticker} {price}", flush=True)

        return jsonify(
            {
                "ok": True,
                "signal": signal,
                "ticker": ticker,
                "price": price,
            }
        ), 200

    except requests.RequestException as error:
        print(f"Discord request failed: {error}", flush=True)

        return jsonify(
            {
                "ok": False,
                "error": "Discord delivery failed.",
            }
        ), 502

    except Exception as error:
        print(f"Webhook processing failed: {error}", flush=True)

        return jsonify(
            {
                "ok": False,
                "error": "Webhook processing failed.",
            }
        ), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)