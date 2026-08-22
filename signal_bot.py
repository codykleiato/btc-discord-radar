def consensus(markets: list[dict]) -> tuple[str, int, int, int]:
    """
    Creates ONE signal from Coinbase, Kraken, and Bitstamp.

    BET UP: all 3 markets are green.
    BET DOWN: all 3 markets are red.
    NO TRADE: mixed market direction or a missing market.
    """
    required_markets = {"Coinbase", "Kraken", "Bitstamp"}
    received_markets = {market["name"] for market in markets}

    if not required_markets.issubset(received_markets):
        return "NO TRADE", 0, 0, 0

    selected_markets = [
        market
        for market in markets
        if market["name"] in required_markets
    ]

    up_count = 0
    down_count = 0

    for market in selected_markets:
        direction, _ = market_direction(market)

        if direction == "UP":
            up_count += 1
        elif direction == "DOWN":
            down_count += 1

    total = len(selected_markets)

    if up_count == 3:
        return "UP", up_count, down_count, total

    if down_count == 3:
        return "DOWN", up_count, down_count, total

    return "NO TRADE", up_count, down_count, total


def send_discord_candle_alert(
    event: str,
    window_start: datetime,
    markets: list[dict],
) -> tuple[bool, Optional[str]]:

    if not DISCORD_WEBHOOK_URL:
        return False, "DISCORD_WEBHOOK_URL is missing."

    required_markets = {"Coinbase", "Kraken", "Bitstamp"}

    selected_markets = [
        market
        for market in markets
        if market["name"] in required_markets
    ]

    signal_type, up_count, down_count, total_markets = consensus(selected_markets)

    if signal_type == "UP":
        emoji = "🟢"
        color = 0x2ECC71
        signal_text = "BET UP"
        signal_title = "BTC UP SIGNAL"
    elif signal_type == "DOWN":
        emoji = "🔴"
        color = 0xE74C3C
        signal_text = "BET DOWN"
        signal_title = "BTC DOWN SIGNAL"
    else:
        emoji = "⚪"
        color = 0x95A5A6
        signal_text = "NO TRADE"
        signal_title = "BTC NO-TRADE SIGNAL"

    event_text = (
        "NEW 15-MINUTE CANDLE OPENED"
        if event == "OPEN"
        else "15-MINUTE CANDLE CLOSED"
    )

    market_text = []

    for market in selected_markets:
        direction, change_pct = market_direction(market)

        direction_emoji = (
            "🟢" if direction == "UP"
            else "🔴" if direction == "DOWN"
            else "⚪"
        )

        market_text.append(
            f"{direction_emoji} **{market['name']}**\n"
            f"Open: {money(market['open'])}\n"
            f"Close: {money(market['close'])}\n"
            f"Change: **{percent(change_pct)}**"
        )

    market_summary = "\n\n".join(market_text)

    payload = {
        "username": "BTC 15m Radar",
        "embeds": [
            {
                "title": f"{emoji} {signal_title}",
                "description": (
                    "**BTC 15-Minute Market Radar**\n"
                    "Combined confirmation from Coinbase, Kraken, and Bitstamp."
                ),
                "color": color,
                "fields": [
                    {
                        "name": "📊 CONSENSUS SIGNAL",
                        "value": (
                            f"**{signal_text}**\n"
                            f"Up: **{up_count}** | "
                            f"Down: **{down_count}** | "
                            f"Markets: **{total_markets}/3**"
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
                    {
                        "name": "₿ MARKET CONFIRMATION",
                        "value": market_summary,
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": (
                        "Strict rule: all 3 markets must agree for BET UP or BET DOWN. "
                        "Informational only — not financial advice."
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