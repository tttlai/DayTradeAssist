"""On-demand health check, triggered by sending /health to the bot
(see telegram_commands.py + main.py). Reports two independent things:

  1. Is Angel One's server reachable at all (raw HTTP, no credentials)?
  2. Does login actually succeed (your API key/PIN/TOTP + their auth
     service both working)?

Getting a reply at all is itself proof the Railway service executed
this run successfully.
"""
import requests

from . import config
from .angel_api import AngelAPI


def check_angel_reachable() -> tuple[bool, str]:
    """Raw HTTP check against Angel's public scrip-master endpoint --
    no credentials needed, just confirms their servers respond."""
    try:
        resp = requests.get(config.SCRIP_MASTER_URL, timeout=15)
        return resp.status_code == 200, f"HTTP {resp.status_code}"
    except requests.RequestException as e:
        return False, f"unreachable ({e.__class__.__name__})"


def check_angel_login() -> tuple[bool, str]:
    """Full auth check -- confirms your API key/PIN/TOTP secret actually
    work, not just that Angel's servers are up."""
    api = AngelAPI()
    try:
        session = api.login()
        api.logout()
        if session.get("status"):
            return True, "login OK"
        return False, f"login rejected: {session.get('message', 'unknown')}"
    except Exception as e:
        return False, f"login failed ({e.__class__.__name__}: {e})"


def run_health_check() -> str:
    reachable, reach_detail = check_angel_reachable()
    login_ok, login_detail = check_angel_login()

    lines = ["*Health Check*", ""]
    lines.append(f"{'✅' if reachable else '❌'} Angel One reachable — {reach_detail}")
    lines.append(f"{'✅' if login_ok else '❌'} Angel One login — {login_detail}")
    lines.append("✅ Railway service is up — this reply is proof the current run executed")
    return "\n".join(lines)
