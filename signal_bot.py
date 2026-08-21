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

last_price = None
last_checked_at = None
last_alert_state = None


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
        print("DISCORD_WEBHOOK_URL is not set; alert was skipped.")
        return

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"content": message},
        timeout=15,
    )
    response.raise_for_status()


def check_btc_price():
    global last_price, last_checked_at, last_alert_state

    try:
        price = get_btc_price()
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        last_price = price
        last_checked_at = checked_at

        is_above_target = price >= BTC_ALERT_ABOVE

        print(
            f"[{checked_at}] BTC price: ${price:,.2f} | "
            f"Target: ${BTC_ALERT_ABOVE:,.2f}"
        )

        if is_above_target and last_alert_state is not True:
            send_discord_message(
                f"🚨 **Bitcoin Price Alert**\n"
                f"BTC is **${price:,.2f} USD**.\n"
                f"It is above your target of **${BTC_ALERT_ABOVE:,.2f} USD**."
            )
            print("Above-target Discord alert sent.")

        elif not is_above_target and last_alert_state is True:
            send_discord_message(
                f"📉 **Bitcoin Price Update**\n"
                f"BTC is **${price:,.2f} USD**.\n"
                f"It is back below **${BTC_ALERT_ABOVE:,.2f} USD**."
            )
            print("Below-target Discord alert sent.")

        last_alert_state = is_above_target

    except requests.RequestException as error:
        print(f"BTC price or Discord request failed: {error}")

    except (KeyError, TypeError, ValueError) as error:
        print(f"Invalid BTC API response: {error}")

    except Exception as error:
        print(f"Unexpected monitor error: {error}")


def price_monitor():
    while True:
        check_btc_price()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    monitor_thread = threading.Thread(target=price_monitor, daemon=True)
    monitor_thread.start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)