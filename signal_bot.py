import os
import json
import math
import urllib.request
from datetime import datetime, timezone

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

BINANCE_URL = (
    "https://api.binance.com/api/v3/klines"
    "?symbol=BTCUSDT&interval=15m&limit=200"
)

def get_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "BTC-Discord-Radar/1.0"}
    )

    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())


def ema(values, period):
    multiplier = 2 / (period + 1)
    result = [values[0]]

    for price in values[1:]:
        result.append(
            (price - result[-1]) * multiplier + result[-1]
        )

    return result


def rsi(values, period=14):
    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        return 100

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(values):
    ema12 = ema(values, 12)
    ema26 = ema(values, 26)

    macd_line = [
        a - b for a, b in zip(ema12, ema26)
    ]

    signal_line = ema(macd_line, 9)

    return macd_line[-1], signal_line[-1]


def send_discord(embed):
    payload = {
        "username": "PKLA BTC Radar",
        "embeds": [embed]
    }

    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "PKLA-BTC-Radar/1.0"
        },
        method="POST"
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        print("Discord response:", response.status)


def main():

    candles = get_json(BINANCE_URL)

    # Ignore the currently forming candle
    closed = candles[:-1]

    closes = [float(c[4]) for c in closed]
    highs = [float(c[2]) for c in closed]
    lows = [float(c[3]) for c in closed]
    volumes = [float(c[5]) for c in closed]

    price = closes[-1]

    ema9 = ema(closes, 9)[-1]
    ema21 = ema(closes, 21)[-1]

    current_rsi = rsi(closes, 14)

    macd_value, macd_signal = macd(closes)

    current_volume = volumes[-1]
    average_volume = sum(volumes[-21:-1]) / 20

    # Previous-day high/low
    day_high = max(highs[-96:-1])
    day_low = min(lows[-96:-1])

    score = 0
    reasons = []

    # EMA trend
    if ema9 > ema21:
        score += 1
        reasons.append("💚 EMA9 > EMA21 — bullish")
    else:
        score -= 1
        reasons.append("❤️ EMA9 < EMA21 — bearish")

    # RSI
    if current_rsi < 30:
        score += 2
        reasons.append("💚 RSI oversold")
    elif current_rsi > 70:
        score -= 2
        reasons.append("❤️ RSI overbought")
    elif current_rsi >= 50:
        score += 1
        reasons.append("🟢 RSI bullish")
    else:
        score -= 1
        reasons.append("🔴 RSI bearish")

    # MACD
    if macd_value > macd_signal:
        score += 2
        reasons.append("💚 MACD bullish")
    else:
        score -= 2
        reasons.append("❤️ MACD bearish")

    # Volume
    if current_volume > average_volume:
        if closes[-1] > closes[-2]:
            score += 1
            reasons.append("💚 Volume confirms upward move")
        else:
            score -= 1
            reasons.append("❤️ Volume confirms downward move")
    else:
        reasons.append("⚪ Volume below average")

    # Previous-day breakout
    if price > day_high:
        score += 2
        reasons.append("💚 Price above previous-day high")
    elif price < day_low:
        score -= 2
        reasons.append("❤️ Price below previous-day low")

    # Convert score to signal
    if score >= 3:
        signal = "⬆️ BET UP"
    elif score <= -3:
        signal = "⬇️ BET DOWN"
    else:
        signal = "⏸️ NO TRADE"

    confidence = min(95, max(50, 50 + abs(score) * 7))

    if signal == "⏸️ NO TRADE":
        confidence = 50

    now = datetime.now(timezone.utc)

    description = (
        "**15-Minute UP / DOWN Market Signal**\n"
        "Automated BTC technical analysis"
    )

    indicator_text = "\n".join(reasons)

    embed = {
        "title": "🚨 PKLA BTC KALSHI SIGNAL",
        "description": description,
        "fields": [
            {
                "name": "₿ BTC Price",
                "value": f"**${price:,.2f}**",
                "inline": False
            },
            {
                "name": "📊 Signal",
                "value": f"**{signal}**",
                "inline": False
            },
            {
                "name": "🎯 Confidence",
                "value": f"**{confidence}%** — Score {score:+d}/10",
                "inline": False
            },
            {
                "name": "📐 Key Levels",
                "value": (
                    f"PDH **${day_high:,.2f}**\n"
                    f"PDL **${day_low:,.2f}**"
                ),
                "inline": False
            },
            {
                "name": "🔬 Indicator Breakdown",
                "value": (
                    f"RSI **{current_rsi:.1f}**\n"
                    f"MACD **{macd_value:.2f}** vs "
                    f"signal **{macd_signal:.2f}**\n"
                    f"EMA9 **${ema9:,.2f}**\n"
                    f"EMA21 **${ema21:,.2f}**\n\n"
                    + indicator_text
                ),
                "inline": False
            },
            {
                "name": "⚠️ Risk Reminder",
                "value": (
                    "Technical signal only. "
                    "No outcome is guaranteed. "
                    "Use proper bankroll management."
                ),
                "inline": False
            }
        ],
        "footer": {
            "text": "PKLA Signal Hub • RSI • MACD • EMA • Volume"
        },
        "timestamp": now.isoformat()
    }

    send_discord(embed)


if __name__ == "__main__":
    main()
