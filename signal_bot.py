def build_embed(result):

    signal = result["signal"]
    direction = result["direction"]
    confidence = result["confidence"]
    price = result["price"]

    if direction == "UP":
        title = "🟢 BTC UP SIGNAL"
        color = 5763719

    elif direction == "DOWN":
        title = "🔴 BTC DOWN SIGNAL"
        color = 15548997

    else:
        title = "⚪ BTC NO TRADE"
        color = 9807270

    reasons_text = "\n".join(
        result["reasons"]
    )

    embed = {
        "title": title,

        "description": (
            "**PKLA BTC 15-Minute Market Radar**\n"
            "Automated technical analysis"
        ),

        "color": color,

        "fields": [

            {
                "name": "₿ BTC PRICE",
                "value": f"**${price:,.2f}**",
                "inline": False
            },

            {
                "name": "📊 SIGNAL",
                "value": f"**{signal}**",
                "inline": False
            },

            {
                "name": "🎯 CONFIDENCE",
                "value": f"**{confidence}%**",
                "inline": False
            },

            {
                "name": "🕯️ HOLD",
                "value": (
                    f"**{result['hold_candles']} candle(s)**"
                ),
                "inline": False
            },

            {
                "name": "📈 SCORE",
                "value": (
                    f"Bullish: **{result['bullish_score']}**\n"
                    f"Bearish: **{result['bearish_score']}**\n"
                    f"Gap: **{result['score_gap']}**"
                ),
                "inline": False
            },

            {
                "name": "📐 INDICATORS",
                "value": (
                    f"RSI: **{result['rsi']:.1f}**\n"
                    f"MACD: **{result['macd']:.2f}**\n"
                    f"Signal: **{result['macd_signal']:.2f}**\n"
                    f"EMA9: **${result['ema9']:,.2f}**\n"
                    f"EMA21: **${result['ema21']:,.2f}**\n"
                    f"EMA40: **${result['ema40']:,.2f}**"
                ),
                "inline": False
            },

            {
                "name": "📍 TRADE LEVELS",
                "value": (
                    f"Entry: **${result['entry']:,.2f}**\n"
                    f"Take Profit: **${result['take_profit']:,.2f}**\n"
                    f"Stop Loss: **${result['stop_loss']:,.2f}**"
                ),
                "inline": False
            },

            {
                "name": "🔬 ANALYSIS",
                "value": reasons_text,
                "inline": False
            }

        ],

        "footer": {
            "text": (
                "PKLA Signal Hub • "
                "BTC • RSI • MACD • EMA • Volume"
            )
        },

        "timestamp": datetime.now(
            timezone.utc
        ).isoformat()
    }

    return embed