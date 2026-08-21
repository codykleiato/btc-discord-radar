import os
import time
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

app = Flask(__name__)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "").strip()

try:
    BTC_ALERT_ABOVE = float(os.getenv("BTC_ALERT_ABOVE", "100000"))
except ValueError:
    BTC_ALERT_ABOVE = 100000.0

try:
    CHECK_INTERVAL_SECONDS = max(30, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
except ValueError:
    CHECK_INTERVAL_SECONDS = 60

last_price = None
last_checked_at = None
last_error = None
last_alert_state = None
monitor_started = False


@app.route("/")
def home():
    return jsonify(
        {
            "service": "btc-discord-radar",
            "status": "running",
            "last_price_usd": last_price,
            "last_checked_at": last_checked_at,
            "alert_above_usd": BTC_ALERT_ABOVE,
            "last_error": last_error,
        }
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


def get_btc_price():
    headers = {}

    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

    response = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": "bitcoin",
            "vs_currencies": "usd",
        },
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()

    data = response.json()
    return float(data["bitcoin"]["usd"])


def send_discord_message(message):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not set; Discord alert skipped.", flush=True)
        return False

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"content": message},
        timeout=20,
    )
    response.raise_for_status()
    return True


def check_btc_price():
    global last_price, last_checked_at, last_error, last_alert_state

    try:
        price = get_btc_price()
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        last_price = price
        last_checked_at = checked_at
        last_error = None

        is_above_target = price >= BTC_ALERT_ABOVE

        print(
            f"[{checked_at}] BTC: ${price:,.2f} | "
            f"Target: ${BTC_ALERT_ABOVE:,.2f}",
            flush=True,
        )

        if is_above_target and last_alert_state is not True:
            sent = send_discord_message(
                f"🚨 **Bitcoin Price Alert**\n"
                f"BTC price: **${price:,.2f} USD**\n"
                f"Target: **${BTC_ALERT_ABOVE:,.2f} USD**"
            )
            if sent:
                print("Above-target Discord alert sent.", flush=True)

        elif not is_above_target and last_alert_state is True:
            sent = send_discord_message(
                f"📉 **Bitcoin Price Update**\n"
                f"BTC price: **${price:,.2f} USD**\n"
                f"BTC is back below **${BTC_ALERT_ABOVE:,.2f} USD**."
            )
            if sent:
                print("Below-target Discord update sent.", flush=True)

        last_alert_state = is_above_target

    except requests.RequestException as error:
        last_error = f"Request error: {error}"
        print(last_error, flush=True)

    except (KeyError, TypeError, ValueError) as error:
        last_error = f"Price-data error: {error}"
        print(last_error, flush=True)

    except Exception as error:
        last_error = f"Unexpected error: {error}"
        print(last_error, flush=True)


def price_monitor():
    print(
        f"BTC monitor started; checking every {CHECK_INTERVAL_SECONDS} seconds.",
        flush=True,
    )

    while True:
        check_btc_price()
        time.sleep(CHECK_INTERVAL_SECONDS)


def start_monitor():
    global monitor_started

    if monitor_started:
        return

    monitor_started = True

    thread = threading.Thread(
        target=price_monitor,
        name="btc-price-monitor",
        daemon=True,
    )
    thread.start()


start_monitor()