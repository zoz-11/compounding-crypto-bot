"""Optional Telegram notifications + command polling (stdlib only).

No-ops silently when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset, so the
bot runs fine without Telegram. Never raises -- notification failures must
never take down the trading loop.

Env vars:
  TELEGRAM_BOT_TOKEN  from @BotFather
  TELEGRAM_CHAT_ID    your chat id (message the bot, then check getUpdates)
"""

import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("notify")

_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
_offset = 0  # getUpdates cursor


def enabled():
    return bool(_TOKEN and _CHAT_ID)


def _api(method, params, timeout=10):
    url = f"https://api.telegram.org/bot{_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def send(text):
    """Send a message. Silently no-ops if unconfigured; never raises."""
    if not enabled():
        return
    try:
        _api("sendMessage", {"chat_id": _CHAT_ID, "text": text})
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


def get_commands():
    """Poll for new text commands from the configured chat. Returns a list
    of message strings (e.g. ["/resume"]). Never raises."""
    global _offset
    if not enabled():
        return []
    try:
        resp = _api("getUpdates", {"offset": _offset + 1, "timeout": 0})
        cmds = []
        for upd in resp.get("result", []):
            _offset = max(_offset, upd["update_id"])
            msg = upd.get("message") or {}
            if str(msg.get("chat", {}).get("id")) == str(_CHAT_ID) and msg.get("text"):
                cmds.append(msg["text"].strip())
        return cmds
    except Exception as e:
        log.warning("Telegram poll failed: %s", e)
        return []
