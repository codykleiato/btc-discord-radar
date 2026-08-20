def send_discord(embed):

    if not DISCORD_WEBHOOK:
        print("ERROR: DISCORD_WEBHOOK is missing.")
        return False

    payload = {
        "username": "Cash Gang BTC Radar",
        "embeds": [embed]
    }

    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        DISCORD_WEBHOOK,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "PKLA-BTC-Radar/2.0"
        },
        method="POST"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=15
        ) as response:

            response_body = response.read().decode("utf-8", errors="replace")

            print("==========================================")
            print("DISCORD SUCCESS")
            print("HTTP STATUS:", response.status)
            print("RESPONSE:", response_body)
            print("==========================================")

            return True

    except urllib.error.HTTPError as error:

        error_body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        print("==========================================")
        print("DISCORD HTTP ERROR")
        print("STATUS:", error.code)
        print("REASON:", error.reason)
        print("RESPONSE:", error_body)
        print("==========================================")

        return False

    except urllib.error.URLError as error:

        print("==========================================")
        print("DISCORD NETWORK ERROR")
        print("ERROR:", error.reason)
        print("==========================================")

        return False

    except Exception as error:

        print("==========================================")
        print("DISCORD UNKNOWN ERROR")
        print("ERROR:", repr(error))
        print("==========================================")

        return False