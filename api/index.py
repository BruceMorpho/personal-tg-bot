"""
Anonymous relay bot - Vercel (serverless webhook) version.

Env vars (set in Vercel project settings):
    BOT_TOKEN        token from @BotFather
    OWNER_ID         your numeric Telegram user ID
    WEBHOOK_SECRET   random string; must match the secret_token used in setWebhook
    KV_REST_API_URL / KV_REST_API_TOKEN   (added automatically by Upstash Redis on Vercel;
    UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN are also accepted)
"""

import logging
import os

import httpx
from flask import Flask, request

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
REDIS_URL = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")

ROUTE_TTL = 60 * 60 * 24 * 90  # keep reply routes for 90 days

# Auto "please wait" message sent to the visitor after they message the bot.
# Set WAITING_MESSAGE to an empty value to disable. Sent once per conversation:
# it is not repeated until you reply to them, or WAITING_COOLDOWN seconds pass.
WAITING_MESSAGE = os.environ.get(
    "WAITING_MESSAGE",
    "Thanks for your message! I've received it and will reply as soon as I can. Please wait a little.",
)
WAITING_COOLDOWN = int(os.environ.get("WAITING_COOLDOWN", 6 * 60 * 60))  # seconds

app = Flask(__name__)
log = logging.getLogger("relay_bot")
logging.basicConfig(level=logging.INFO)


# ---------- Telegram Bot API ----------
def tg(method: str, **params) -> dict:
    r = httpx.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=20
    )
    data = r.json()
    if not data.get("ok"):
        log.warning("Telegram %s failed: %s", method, data)
    return data


# ---------- Redis (Upstash REST) ----------
def redis(*command):
    r = httpx.post(
        REDIS_URL,
        headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
        json=list(command),
        timeout=10,
    )
    return r.json().get("result")


def save_route(owner_msg_id: int, user_chat_id: int, user_msg_id: int):
    redis("SET", f"route:{owner_msg_id}", f"{user_chat_id}:{user_msg_id}", "EX", ROUTE_TTL)


def get_route(owner_msg_id: int):
    value = redis("GET", f"route:{owner_msg_id}")
    if not value:
        return None
    chat_id, msg_id = value.split(":")
    return int(chat_id), int(msg_id)


def should_send_waiting(user_chat_id: int) -> bool:
    """True only the first time within the cooldown (Redis SET NX)."""
    if not WAITING_MESSAGE.strip():
        return False
    if WAITING_COOLDOWN <= 0:
        return True
    return redis("SET", f"wait:{user_chat_id}", "1", "NX", "EX", WAITING_COOLDOWN) == "OK"


def reset_waiting(user_chat_id: int):
    redis("DEL", f"wait:{user_chat_id}")


# ---------- logic ----------
def handle_visitor(msg: dict):
    chat_id = msg["chat"]["id"]

    if msg.get("text", "").startswith("/"):
        if msg["text"].startswith("/start"):
            tg("sendMessage", chat_id=chat_id,
               text="Hi! Send me a message (text, photo, video, etc.) and I'll pass it on.")
        return

    user = msg.get("from", {})
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Unknown"
    handle = f" (@{user['username']})" if user.get("username") else ""

    header = tg(
        "sendMessage",
        chat_id=OWNER_ID,
        text=f"📩 From: {name}{handle}\nID: {user.get('id')}\n↩️ Reply to this or the message below.",
    )
    copied = tg(
        "copyMessage",
        chat_id=OWNER_ID,
        from_chat_id=chat_id,
        message_id=msg["message_id"],
    )

    for res in (header, copied):
        if res.get("ok"):
            save_route(res["result"]["message_id"], chat_id, msg["message_id"])

    # Auto reply so the visitor knows the message arrived (only if it reached the owner)
    if copied.get("ok") and should_send_waiting(chat_id):
        tg("sendMessage", chat_id=chat_id, text=WAITING_MESSAGE,
           reply_parameters={"message_id": msg["message_id"], "allow_sending_without_reply": True})


def handle_owner(msg: dict):
    owner_chat = msg["chat"]["id"]

    if msg.get("text", "").startswith("/start"):
        tg("sendMessage", chat_id=owner_chat,
           text="Relay is active. Reply to any relayed message and I'll deliver your reply anonymously.")
        return
    if msg.get("text", "").startswith("/"):
        return

    replied = msg.get("reply_to_message")
    if not replied:
        tg("sendMessage", chat_id=owner_chat,
           text="To respond to someone, use Telegram's Reply on their message.")
        return

    route = get_route(replied["message_id"])
    if not route:
        tg("sendMessage", chat_id=owner_chat,
           text="I can't tell who that message came from. Reply to a relayed message.")
        return

    user_chat_id, user_msg_id = route
    # copyMessage sends as the bot: no forward tag, nothing about the owner is exposed
    res = tg(
        "copyMessage",
        chat_id=user_chat_id,
        from_chat_id=owner_chat,
        message_id=msg["message_id"],
        reply_parameters={"message_id": user_msg_id, "allow_sending_without_reply": True},
    )
    if res.get("ok"):
        reset_waiting(user_chat_id)  # next message from them gets the waiting notice again
        tg("setMessageReaction", chat_id=owner_chat, message_id=msg["message_id"],
           reaction=[{"type": "emoji", "emoji": "👍"}])
    else:
        tg("sendMessage", chat_id=owner_chat,
           text=f"❌ Couldn't deliver: {res.get('description', 'unknown error')}")


@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def webhook(path):
    if request.method == "GET":
        return "relay bot is running", 200

    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return "forbidden", 403

    try:
        update = request.get_json(force=True, silent=True) or {}
        msg = update.get("message")
        if msg and msg["chat"]["type"] == "private":
            if msg["chat"]["id"] == OWNER_ID:
                handle_owner(msg)
            else:
                handle_visitor(msg)
    except Exception:
        log.exception("Error handling update")

    # Always return 200 so Telegram doesn't keep retrying a failing update
    return "ok", 200
    
