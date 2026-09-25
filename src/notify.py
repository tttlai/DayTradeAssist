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
            if resp.status_code == 400:
                # Most likely malformed Markdown -- some dynamic value
                # (a stray _ or * in interpolated text) broke Telegram's
                # entity parser (confirmed in production: tradesim's
                # "TIME_EXIT"/"NO_TRIGGER" outcome strings did exactly
                # this). Retry as plain text so the message still reaches
                # you -- an unformatted message beats a silently lost one,
                # and this catches any future formatting bug the same way,
                # not just this specific one (which is separately fixed
                # at the source in report.py).
                print("[notify] retrying as plain text (likely a Markdown formatting issue)")
                retry_resp = requests.post(
                    url,
                    json={
                        "chat_id": config.TELEGRAM_CHAT_ID,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                    timeout=15,
                )
                if not retry_resp.ok:
                    print(f"[notify] plain-text retry also failed: {retry_resp.status_code} {retry_resp.text}")
    except requests.RequestException as e:
        print(f"[notify] Telegram send raised {e.__class__.__name__}: {e}")
