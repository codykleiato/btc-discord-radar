import os
import time
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

app = Flask(__name__)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
BTC_ALERT_ABOVE = float(os.getenv("BTC_ALERT_ABOVE", "100000"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

last_alert_state = None
last_price = None
last_checked_at = None


@app.route("/")
def home():
    return jsonify(
        {
            "service": "btc-discord-radar",
            "status": "running",
            "last_price_usd": last_price,
            "last_checked_at": last_checked_at,
            "alert_above_usd": BTC_ALERT_ABOVE,
        }
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


def get_btc_price():
    response = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": "bitcoin",
            "vs_currencies": "usd",
        },
        timeout=15,
    )
    response.raise_for_status()

    data = response.json()
    return float(data["bitcoin"]["usd"])


def send_discord_message(message):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is missing; Discord message was not sent.")
        return

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"content": message},
        timeout=15,
    )
    response.raise_for_status()


def check_btc_price():
    global last_alert_state, last_price, last_checked_at

    try:
        price = get_btc_price()
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        last_price = price
        last_checked_at = checked_at

        above_threshold = price >= BTC_ALERT_ABOVE

        print(
            f"[{checked_at}] BTC: ${price:,.2f}; "
            f"alert level: ${BTC_ALERT_ABOVE:,.2f}"
        )

        if above_threshold and last_alert_state is not True:
            send_discord_message(
                f"🚨 **BTC Price Alert**\n"
                f"Bitcoin: **${price:,.2f} USD**\n"
                f"Above target: **${BTC_ALERT_ABOVE:,.2f} USD**"
            )
            print("Above-threshold Discord alert sent.")

        if not above_threshold and last_alert_state is True:
            send_discord_message(
                f"📉 **BTC Price Update**\n"
                f"Bitcoin: **${price:,.2f} USD**\n"
                f"Below target: **${BTC_ALERT_ABOVE:,.2f} USD**"
            )
            print("Below-threshold Discord update sent.")

        last_alert_state = above_threshold

    except requests.RequestException as error:
        print(f"BTC or Discord request failed: {error}")

    except (KeyError, TypeError, ValueError) as error:
        print(f"Invalid BTC price response: {error}")

    except Exception as error:
        print(f"Unexpected monitor error: {error}")


def price_monitor():
    while True:
        check_btc_price()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    threading.Thread(target=price_monitor, daemon=True).start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)