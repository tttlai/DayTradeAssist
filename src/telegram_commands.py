"""Polls Telegram for new messages sent to the bot, using getUpdates with
an offset so each message is processed exactly once across runs (the
offset cursor is persisted on disk -- see main.py's telegram state file,
kept separate from the daily-reset trading state since update IDs are
not tied to any particular trading day).
"""
import requests

from . import config


def get_new_messages(last_update_id: int):
    """Returns (list of message texts from YOUR chat, new last_update_id).
    Silently no-ops if Telegram isn't configured."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return [], last_update_id

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 0}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[telegram] getUpdates failed: {e}")
        return [], last_update_id

    if not data.get("ok"):
        return [], last_update_id

    updates = data.get("result", [])
    new_last_id = last_update_id
    messages = []
    for u in updates:
        new_last_id = max(new_last_id, u["update_id"])
        msg = u.get("message", {})
        text = msg.get("text", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))
        # Only react to messages from your own configured chat -- anyone
        # else who finds the bot's username shouldn't be able to trigger it.
        if text and chat_id == str(config.TELEGRAM_CHAT_ID):
            messages.append(text.strip())

    return messages, new_last_id
