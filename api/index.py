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

VISITOR_WELCOME = "Hi! Send your message here. The admin will reply soon, please wait."

OWNER_HELP = (
    "Relay is active. Reply to any relayed message to answer anonymously.\n\n"
    "/users - list users\n"
    "/ban - ban user\n"
    "/unban - unban user\n"
    "/bans - banned list\n"
    "/broadcast - message everyone\n"
    "/delete - (reply to your sent message) delete it from the user's chat"
)

OWNER_COMMANDS = [
    {"command": "users", "description": "List users"},
    {"command": "ban", "description": "Ban user"},
    {"command": "unban", "description": "Unban user"},
    {"command": "bans", "description": "Banned list"},
    {"command": "broadcast", "description": "Message everyone"},
    {"command": "delete", "description": "Delete a sent message from user's chat"},
]

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


def say(chat_id: int, text: str):
    tg("sendMessage", chat_id=chat_id, text=text[:4000])


def is_banned(user_id: int) -> bool:
    return bool(redis("SISMEMBER", "banned", user_id))


def register_user(msg: dict):
    user = msg.get("from", {})
    uid = user.get("id") or msg["chat"]["id"]
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Unknown"
    handle = f" @{user['username']}" if user.get("username") else ""
    redis("SADD", "users", uid)
    redis("SET", f"uname:{uid}", f"{name}{handle}")


def label(uid) -> str:
    return f"{redis('GET', f'uname:{uid}') or 'Unknown'} ({uid})"


# ---------- logic ----------
def handle_visitor(msg: dict):
    chat_id = msg["chat"]["id"]

    if is_banned(chat_id):
        return  # banned users get no reply at all

    if msg.get("text", "").startswith("/"):
        # Admin commands (/ban, /users ...) get NO reply for normal users
        if msg["text"].startswith("/start"):
            register_user(msg)
            say(chat_id, VISITOR_WELCOME)
        return

    register_user(msg)
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


# ---------- owner-only commands ----------
def target_user(msg: dict, args: list):
    """User id from '/cmd <user_id>' or from replying to a relayed message."""
    if args:
        try:
            return int(args[0])
        except ValueError:
            return None
    replied = msg.get("reply_to_message")
    if replied:
        route = get_route(replied["message_id"])
        if route:
            return route[0]
    return None


def owner_start(msg, args):
    chat = msg["chat"]["id"]
    say(chat, OWNER_HELP)
    # Menu button: empty for everyone, full list only for the owner
    tg("deleteMyCommands")
    tg("setMyCommands", commands=OWNER_COMMANDS,
       scope={"type": "chat", "chat_id": OWNER_ID})


def cmd_users(msg, args):
    chat = msg["chat"]["id"]
    ids = redis("SMEMBERS", "users") or []
    if not ids:
        say(chat, "No users yet.")
        return
    lines = []
    for uid in ids:
        mark = " [banned]" if is_banned(uid) else ""
        lines.append(f"{label(uid)}{mark}")
    say(chat, f"Users ({len(lines)}):\n" + "\n".join(lines))


def cmd_ban(msg, args):
    chat = msg["chat"]["id"]
    uid = target_user(msg, args)
    if not uid:
        say(chat, "Usage: /ban <user_id>, or reply to a relayed message.")
        return
    if uid == OWNER_ID:
        say(chat, "You can't ban yourself.")
        return
    redis("SADD", "banned", uid)
    say(chat, f"Banned: {label(uid)}")


def cmd_unban(msg, args):
    chat = msg["chat"]["id"]
    uid = target_user(msg, args)
    if not uid:
        say(chat, "Usage: /unban <user_id>, or reply to a relayed message.")
        return
    if redis("SREM", "banned", uid):
        say(chat, f"Unbanned: {label(uid)}")
    else:
        say(chat, "That user is not banned.")


def cmd_bans(msg, args):
    chat = msg["chat"]["id"]
    ids = redis("SMEMBERS", "banned") or []
    if not ids:
        say(chat, "No banned users.")
        return
    say(chat, "Banned users:\n" + "\n".join(label(u) for u in ids))


def cmd_broadcast(msg, args):
    chat = msg["chat"]["id"]
    source = msg.get("reply_to_message")
    parts = (msg.get("text") or "").split(None, 1)
    if not source and len(parts) < 2:
        say(chat, "Usage: /broadcast <text>, or reply to any message with /broadcast.")
        return
    ok = failed = 0
    for uid in redis("SMEMBERS", "users") or []:
        if is_banned(uid):
            continue
        if source:
            res = tg("copyMessage", chat_id=int(uid), from_chat_id=chat,
                     message_id=source["message_id"])
        else:
            res = tg("sendMessage", chat_id=int(uid), text=parts[1])
        if res.get("ok"):
            ok += 1
        else:
            failed += 1
    say(chat, f"Broadcast done. Sent: {ok}, failed: {failed}.")


def cmd_delete(msg, args):
    chat = msg["chat"]["id"]
    replied = msg.get("reply_to_message")
    value = redis("GET", f"sent:{replied['message_id']}") if replied else None
    if not value:
        say(chat, "Reply to a message you sent (via the bot) with /delete.")
        return
    user_chat_id, user_msg_id = map(int, value.split(":"))
    res = tg("deleteMessage", chat_id=user_chat_id, message_id=user_msg_id)
    if res.get("ok"):
        redis("DEL", f"sent:{replied['message_id']}")
        say(chat, "Deleted from the user's chat.")
    else:
        say(chat, f"Couldn't delete: {res.get('description', 'unknown error')}")


OWNER_CMDS = {
    "/start": owner_start,
    "/users": cmd_users,
    "/ban": cmd_ban,
    "/unban": cmd_unban,
    "/bans": cmd_bans,
    "/broadcast": cmd_broadcast,
    "/delete": cmd_delete,
}


def handle_owner(msg: dict):
    owner_chat = msg["chat"]["id"]

    text = msg.get("text", "") or ""
    if text.startswith("/"):
        parts = text.split()
        handler = OWNER_CMDS.get(parts[0].split("@")[0].lower())
        if handler:
            handler(msg, parts[1:])
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
        redis("SET", f"sent:{msg['message_id']}",
              f"{user_chat_id}:{res['result']['message_id']}", "EX", ROUTE_TTL)
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

