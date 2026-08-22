def send_discord_analysis(
    event: str,
    window_start: datetime,
    markets: list[dict],
) -> tuple[bool, Optional[str]]:

    if not DISCORD_WEBHOOK_URL:
        return False, "DISCORD_WEBHOOK_URL is missing."

    signal_type, up_count, down_count, market_count = consensus(markets)

    if signal_type == "UP":
        emoji = "🟢"
        color = 0x2ECC71
        title = "BTC UP SIGNAL"
        bet_text = "BET UP"
    elif signal_type == "DOWN":
        emoji = "🔴"
        color = 0xE74C3C
        title = "BTC DOWN SIGNAL"
        bet_text = "BET DOWN"
    else:
        emoji = "⚪"
        color = 0x95A5A6
        title = "BTC NO-TRADE SIGNAL"
        bet_text = "NO TRADE"

    if event == "OPEN":
        event_text = "NEW 15-MINUTE CANDLE OPENED"
        analysis_text = "Analysis 1 of 2 — Opening scan"
    elif event == "MID":
        event_text = "7:30 MID-CANDLE CONFIRMATION"
        analysis_text = "Analysis 2 of 2 — Mid-candle scan"
    else:
        event_text = "15-MINUTE CANDLE CLOSED"
        analysis_text = "Closing scan"

    selected_markets = [
        market
        for market in markets
        if market["name"] in {"Coinbase", "Kraken", "Bitstamp"}
    ]

    market_lines = []

    for market in selected_markets:
        direction, change_pct = market_direction(market)

        direction_emoji = (
            "🟢" if direction == "UP"
            else "🔴" if direction == "DOWN"
            else "⚪"
        )

        market_lines.append(
            f"{direction_emoji} **{market['name']}**\n"
            f"Open: {money(market['open'])}\n"
            f"Current: {money(market['close'])}\n"
            f"Change: **{percent(change_pct)}**"
        )

    market_confirmation = "\n\n".join(market_lines)

    confidence = round((max(up_count, down_count) / 3) * 100)

    payload = {
        "username": "PKLA BTC Radar",
        "embeds": [
            {
                "title": f"{emoji} {title}",
                "description": (
                    "**PKLA BTC 15-Minute Market Radar**\n"
                    "Coinbase, Kraken, and Bitstamp combined analysis"
                ),
                "color": color,
                "fields": [
                    {
                        "name": "📊 SIGNAL",
                        "value": f"**{bet_text}**",
                        "inline": True,
                    },
                    {
                        "name": "🎯 CONFIDENCE",
                        "value": f"**{confidence}%**",
                        "inline": True,
                    },
                    {
                        "name": "🕯️ HOLD",
                        "value": "**Until 15m candle close**",
                        "inline": True,
                    },
                    {
                        "name": "📈 SCORE",
                        "value": (
                            f"Bullish: **{up_count}**\n"
                            f"Bearish: **{down_count}**\n"
                            f"Gap: **{up_count - down_count:+d}**\n"
                            f"Markets online: **{market_count}/3**"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "🕒 15-MINUTE WINDOW",
                        "value": (
                            f"Start: **{window_start.strftime('%Y-%m-%d %H:%M UTC')}**\n"
                            f"Event: **{event_text}**\n"
                            f"{analysis_text}"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "🔬 MARKET ANALYSIS",
                        "value": market_confirmation or "Market data unavailable.",
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": (
                        "Strict rule: Coinbase, Kraken, and Bitstamp must all agree "
                        "for BET UP or BET DOWN. Informational only — not financial advice."
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