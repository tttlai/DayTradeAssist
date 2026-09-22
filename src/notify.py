import requests

from . import config


def send_message(text: str):
    """Never raises -- every call site in main.py does real state-tracking
    work (marking a window/day done) right after calling this, inside the
    same try block. If this raised on a genuine network failure (not just
    a bad HTTP response, which was already handled below), it would skip
    past that bookkeeping exactly like the bugs already fixed elsewhere,
    just via this one shared function instead of a per-caller API call.
    Hardening it here protects every caller at once."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("[notify] Telegram not configured; printing report instead:\n")
        print(text)
        return

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"[notify] Telegram send failed: {resp.status_code} {resp.text}")
    except requests.RequestException as e:
        print(f"[notify] Telegram send raised {e.__class__.__name__}: {e}")
