"""
Anonymous relay bot - Vercel (serverless webhook) version.

Owner commands:
    /users                 list everyone who has messaged the bot
    /ban <id>              ban a user (or reply to a relayed message with /ban)
    /unban <id>            unban a user (or reply to a relayed message)
    /bans                  list banned users
    /broadcast <text>      send text to all users (or reply to any message with /broadcast)
    /delete                reply to a message YOU sent via the bot -> deletes it from the user's chat too

Env vars (set in Vercel project settings):
    BOT_TOKEN        token from @BotFather
    OWNER_ID         your numeric Telegram user ID
    WEBHOOK_SECRET   random string; must match the secret_token used in setWebhook
    KV_REST_API_URL / KV_REST_API_TOKEN   (Upstash Redis on Vercel;
    UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN are also accepted)
"""

import json
import logging
import os
import time

import httpx
from flask import Flask, request

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
REDIS_URL = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")

ROUTE_TTL = 60 * 60 * 24 * 90  # keep reply routes for 90 days

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


def say(chat_id: int, text: str):
    """Send text, splitting into chunks to respect Telegram's 4096 char limit."""
    for i in range(0, len(text), 4000):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 4000])


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


# owner's reply message -> the copy delivered to the user (needed for /delete)
def save_sent(owner_msg_id: int, user_chat_id: int, delivered_msg_id: int):
    redis("SET", f"sent:{owner_msg_id}", f"{user_chat_id}:{delivered_msg_id}", "EX", ROUTE_TTL)


def get_sent(owner_msg_id: int):
    value = redis("GET", f"sent:{owner_msg_id}")
    if not value:
        return None
    chat_id, msg_id = value.split(":")
    return int(chat_id), int(msg_id)


def should_send_waiting(user_chat_id: int) -> bool:
    if not WAITING_MESSAGE.strip():
        return False
    if WAITING_COOLDOWN <= 0:
        return True
    return redis("SET", f"wait:{user_chat_id}", "1", "NX", "EX", WAITING_COOLDOWN) == "OK"


def reset_waiting(user_chat_id: int):
    redis("DEL", f"wait:{user_chat_id}")


# ---------- users / bans ----------
def register_user(user: dict):
    uid = user.get("id")
    info = {
        "name": " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Unknown",
        "username": user.get("username"),
    }
    redis("SADD", "users", uid)
    redis("SET", f"user:{uid}", json.dumps(info))


def get_user_info(uid) -> dict:
    raw = redis("GET", f"user:{uid}")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def fmt_user(uid) -> str:
    info = get_user_info(uid)
    name = info.get("name", "Unknown")
    handle = f" @{info['username']}" if info.get("username") else ""
    return f"{name}{handle} — {uid}"


def is_banned(uid: int) -> bool:
    return redis("SISMEMBER", "banned", uid) == 1


def ban(uid: int):
    redis("SADD", "banned", uid)


def unban(uid: int):
    redis("SREM", "banned", uid)


# ---------- visitor ----------
def handle_visitor(msg: dict):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})

    # banned users are silently ignored
    if is_banned(chat_id):
        return

    register_user(user)

    if msg.get("text", "").startswith("/"):
        if msg["text"].startswith("/start"):
            tg("sendMessage", chat_id=chat_id,
               text="Hi! Send me a message (text, photo, video, etc.) and I'll pass it on.")
        return

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

    if copied.get("ok") and should_send_waiting(chat_id):
        tg("sendMessage", chat_id=chat_id, text=WAITING_MESSAGE,
           reply_parameters={"message_id": msg["message_id"], "allow_sending_without_reply": True})


# ---------- owner commands ----------
def target_from(msg: dict, args: list):
    """Find the target user id from /cmd <id> or from a reply to a relayed message."""
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


def cmd_users(chat):
    ids = sorted(redis("SMEMBERS", "users") or [], key=int)
    if not ids:
        return say(chat, "No users yet.")
    banned = set(redis("SMEMBERS", "banned") or [])
    lines = [f"👥 Users: {len(ids)}\n"]
    for n, uid in enumerate(ids, 1):
        flag = " 🚫" if uid in banned else ""
        lines.append(f"{n}. {fmt_user(uid)}{flag}")
    say(chat, "\n".join(lines))


def cmd_bans(chat):
    ids = redis("SMEMBERS", "banned") or []
    if not ids:
        return say(chat, "No banned users.")
    say(chat, "🚫 Banned:\n\n" + "\n".join(f"• {fmt_user(u)}" for u in ids))


def cmd_ban(msg, chat, args, do_ban=True):
    uid = target_from(msg, args)
    if uid is None:
        return say(chat, f"Usage: /{'ban' if do_ban else 'unban'} <user_id>, or reply to a relayed message.")
    if uid == OWNER_ID:
        return say(chat, "You can't ban yourself 🙂")
    if do_ban:
        ban(uid)
        say(chat, f"🚫 Banned: {fmt_user(uid)}")
    else:
        unban(uid)
        say(chat, f"✅ Unbanned: {fmt_user(uid)}")


def cmd_broadcast(msg, chat, text_after):
    replied = msg.get("reply_to_message")
    if not replied and not text_after:
        return say(chat, "Usage: /broadcast <text>, or reply to any message with /broadcast.")

    ids = redis("SMEMBERS", "users") or []
    banned = set(redis("SMEMBERS", "banned") or [])
    sent = failed = 0
    for uid in ids:
        if uid in banned or int(uid) == OWNER_ID:
            continue
        if replied:
            res = tg("copyMessage", chat_id=int(uid), from_chat_id=chat,
                     message_id=replied["message_id"])
        else:
            res = tg("sendMessage", chat_id=int(uid), text=text_after)
        if res.get("ok"):
            sent += 1
        else:
            failed += 1  # e.g. user blocked the bot
        time.sleep(0.05)  # stay under Telegram's ~30 msg/sec limit
    say(chat, f"📢 Broadcast done.\n✅ Sent: {sent}\n❌ Failed: {failed}")


def cmd_delete(msg, chat):
    replied = msg.get("reply_to_message")
    if not replied:
        return say(chat, "Reply to a message you sent (via the bot) with /delete.")
    sent = get_sent(replied["message_id"])
    if not sent:
        return say(chat, "I can't find that message. Only replies sent after this update can be deleted.")
    user_chat_id, delivered_id = sent
    res = tg("deleteMessage", chat_id=user_chat_id, message_id=delivered_id)
    if res.get("ok"):
        tg("deleteMessage", chat_id=chat, message_id=replied["message_id"])
        tg("deleteMessage", chat_id=chat, message_id=msg["message_id"])
    else:
        say(chat, f"❌ Couldn't delete: {res.get('description', 'unknown error')}")


def setup_command_menus():
    """Everyone sees only /start in the menu; the owner sees all admin commands."""
    tg("setMyCommands", commands=[{"command": "start", "description": "Start the bot"}])
    tg("setMyCommands",
       scope={"type": "chat", "chat_id": OWNER_ID},
       commands=[
           {"command": "start", "description": "Start / refresh menu"},
           {"command": "users", "description": "List all users"},
           {"command": "ban", "description": "Ban a user (reply or ID)"},
           {"command": "unban", "description": "Unban a user"},
           {"command": "bans", "description": "List banned users"},
           {"command": "broadcast", "description": "Message all users"},
           {"command": "delete", "description": "Delete a message you sent"},
       ])


def handle_owner(msg: dict):
    owner_chat = msg["chat"]["id"]
    text = msg.get("text", "") or ""

    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        args = rest.split()

        if cmd == "/start":
            setup_command_menus()
            return say(owner_chat,
                "Relay is active. Reply to any relayed message to answer anonymously.\n\n"
                "/users – list users\n/ban – ban user\n/unban – unban user\n"
                "/bans – banned list\n/broadcast – message everyone\n"
                "/delete – (reply to your sent message) delete it from the user's chat")
        if cmd == "/users":
            return cmd_users(owner_chat)
        if cmd == "/bans":
            return cmd_bans(owner_chat)
        if cmd == "/ban":
            return cmd_ban(msg, owner_chat, args, True)
        if cmd == "/unban":
            return cmd_ban(msg, owner_chat, args, False)
        if cmd == "/broadcast":
            return cmd_broadcast(msg, owner_chat, rest)
        if cmd == "/delete":
            return cmd_delete(msg, owner_chat)
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
    res = tg(
        "copyMessage",
        chat_id=user_chat_id,
        from_chat_id=owner_chat,
        message_id=msg["message_id"],
        reply_parameters={"message_id": user_msg_id, "allow_sending_without_reply": True},
    )
    if res.get("ok"):
        save_sent(msg["message_id"], user_chat_id, res["result"]["message_id"])
        reset_waiting(user_chat_id)
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

    return "ok", 200
    
