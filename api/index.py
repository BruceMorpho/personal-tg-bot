"""
Anonymous relay bot - Vercel (serverless webhook) version.
+ Payment flow (QR, screenshot, channel link) with owner-editable settings.

Env vars (set in Vercel project settings):
    BOT_TOKEN        token from @BotFather
    OWNER_ID         your numeric Telegram user ID
    WEBHOOK_SECRET   random string; must match the secret_token used in setWebhook
    KV_REST_API_URL / KV_REST_API_TOKEN   (added automatically by Upstash Redis on Vercel;
    UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN are also accepted)
"""

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

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
    "/delete - (reply to your sent message) delete it from the user's chat\n"
    "/pay - edit offer, UPI ID, QR, channel link\n"
    "/cancelpay - cancel a pending payment edit"
)

OWNER_COMMANDS = [
    {"command": "users", "description": "List users"},
    {"command": "ban", "description": "Ban user"},
    {"command": "unban", "description": "Unban user"},
    {"command": "bans", "description": "Banned list"},
    {"command": "broadcast", "description": "Message everyone"},
    {"command": "delete", "description": "Delete a sent message from user's chat"},
    {"command": "pay", "description": "Payment settings"},
    {"command": "cancelpay", "description": "Cancel a payment edit"},
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
    redis("SREM", "blocked", uid)  # they messaged us, so they are active again


def label(uid) -> str:
    return f"{redis('GET', f'uname:{uid}') or 'Unknown'} ({uid})"


# ---------- payment flow ----------
# Everything the owner can edit lives in Redis under cfg:<key>, so changing the
# offer, UPI ID, QR or channel link never needs a redeploy.
PAY_DEFAULTS = {
    "hi": "Hi! Do you want a subscription?\nReply: I want subscription",
    "offer": "Monthly Rs 199, Yearly Rs 1499",
    "upi": "",
    "qr": "",
    "link": "",
}

SET_PROMPTS = {
    "hi": "Send the new greeting text.",
    "offer": "Send the new offer text.",
    "upi": "Send the new UPI ID.",
    "qr": "Send the new QR as a photo.",
    "link": "Send the new channel link.",
}

TXT_ASK_SS = (
    "After payment, share the screenshot here.\n"
    "Admin will check your payment and send you the channel link."
)
TXT_PAY_WAITING = (
    "Payment screenshot received. Admin will check your payment "
    "and add you or send you the channel link shortly."
)
TXT_REJECTED = "We couldn't find your payment. Please send a clear screenshot again."

PAYFLOW_TTL = 60 * 60 * 24  # how long we wait for a screenshot after "I want to pay"
KEYWORD_MAX_LEN = 60        # longer messages are never treated as keywords

RE_PAY = re.compile(r"\b(pay|payment)\b", re.I)
RE_SUB = re.compile(r"\b(subscription|subscribe|plan)\b", re.I)
RE_HI = re.compile(r"\b(hi|hello|hey|vanakkam)\b", re.I)


def cfg(key: str) -> str:
    return redis("GET", f"cfg:{key}") or PAY_DEFAULTS[key]


def set_cfg(key: str, value: str):
    redis("SET", f"cfg:{key}", value)


def has_image(msg: dict) -> bool:
    doc = msg.get("document") or {}
    return bool(msg.get("photo")) or str(doc.get("mime_type", "")).startswith("image/")


def sender_label(msg: dict) -> str:
    user = msg.get("from", {})
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Unknown"
    handle = f" (@{user['username']})" if user.get("username") else ""
    return f"{name}{handle}\nID: {user.get('id') or msg['chat']['id']}"


def send_screenshot_to_owner(msg: dict) -> bool:
    chat_id = msg["chat"]["id"]
    copied = tg("copyMessage", chat_id=OWNER_ID, from_chat_id=chat_id,
                message_id=msg["message_id"])
    if not copied.get("ok"):
        return False
    header = tg(
        "sendMessage",
        chat_id=OWNER_ID,
        text=f"💰 Payment screenshot\nFrom: {sender_label(msg)}",
        reply_markup={"inline_keyboard": [
            [{"text": "Send channel link", "callback_data": f"pay:link:{chat_id}"}],
            [{"text": "Payment not received", "callback_data": f"pay:rej:{chat_id}"}],
        ]},
    )
    # The owner can also use Telegram's Reply on either message to chat with them
    for res in (copied, header):
        if res.get("ok"):
            save_route(res["result"]["message_id"], chat_id, msg["message_id"])
    return True


def handle_payment(msg: dict) -> bool:
    """True when this message was handled by the payment flow (not relayed)."""
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    # Payment screenshot (only right after "I want to pay")
    if has_image(msg):
        if not redis("GET", f"payflow:{chat_id}"):
            return False
        if not send_screenshot_to_owner(msg):
            return False
        redis("DEL", f"payflow:{chat_id}")
        say(chat_id, TXT_PAY_WAITING)
        return True

    if not text or len(text) > KEYWORD_MAX_LEN:
        return False

    if RE_PAY.search(text):
        qr = redis("GET", "cfg:qr")
        upi = redis("GET", "cfg:upi")  # empty until the owner sets it
        if qr:
            caption = f"Scan to pay: {upi}" if upi else "Scan to pay"
            sent = tg("sendPhoto", chat_id=chat_id, photo=qr, caption=caption)
        elif upi:
            sent = tg("sendMessage", chat_id=chat_id, text=f"Pay to this UPI ID:\n{upi}")
        else:
            return False  # neither QR nor UPI set yet: relay to the owner as usual
        if not sent.get("ok"):
            return False
        say(chat_id, TXT_ASK_SS)
        redis("SET", f"payflow:{chat_id}", "1", "EX", PAYFLOW_TTL)
        return True
    if RE_SUB.search(text):
        say(chat_id, f"Subscription details:\n{cfg('offer')}\nReply: I want to pay")
        return True
    if RE_HI.search(text):
        say(chat_id, cfg("hi"))
        return True
    return False


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

    if handle_payment(msg):
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
    banned = set(redis("SMEMBERS", "banned") or [])
    blocked = set(redis("SMEMBERS", "blocked") or [])
    lines = []
    for uid in ids:
        mark = ""
        if uid in banned:
            mark = " [banned]"
        elif uid in blocked:
            mark = " [blocked bot]"
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

    # Fetch both sets once (instead of one Redis call per user)
    users = set(redis("SMEMBERS", "users") or [])
    banned = set(redis("SMEMBERS", "banned") or [])
    targets = list(users - banned)

    def send(uid):
        for _ in range(3):
            if source:
                res = tg("copyMessage", chat_id=int(uid), from_chat_id=chat,
                         message_id=source["message_id"])
            else:
                res = tg("sendMessage", chat_id=int(uid), text=parts[1])
            if res.get("ok"):
                return True
            code = res.get("error_code")
            if code == 429:  # rate limited: wait and retry
                time.sleep(res.get("parameters", {}).get("retry_after", 1))
                continue
            if code == 403:  # user blocked the bot: only MARK, never delete
                redis("SADD", "blocked", uid)
            return False
        return False

    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(send, targets))

    ok = sum(1 for r in results if r)
    say(chat, f"Broadcast done. Sent: {ok}, failed: {len(results) - ok}.")


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


def settings_text() -> str:
    return (
        "Current settings\n\n"
        f"Greeting: {cfg('hi')}\n"
        f"Offer: {cfg('offer')}\n"
        f"UPI ID: {cfg('upi') or 'not set'}\n"
        f"QR: {'set' if cfg('qr') else 'not set'}\n"
        f"Channel link: {cfg('link') or 'not set'}"
    )


def cmd_pay(msg, args):
    tg(
        "sendMessage",
        chat_id=msg["chat"]["id"],
        text="Payment settings. Choose what to edit.",
        reply_markup={"inline_keyboard": [
            [{"text": "Edit offer", "callback_data": "set:offer"},
             {"text": "Change UPI ID", "callback_data": "set:upi"}],
            [{"text": "Change QR", "callback_data": "set:qr"},
             {"text": "Channel link", "callback_data": "set:link"}],
            [{"text": "Edit greeting", "callback_data": "set:hi"},
             {"text": "Show settings", "callback_data": "set:show"}],
        ]},
    )


def cmd_cancelpay(msg, args):
    redis("DEL", "admin_state")
    say(msg["chat"]["id"], "Cancelled.")


def apply_admin_state(msg: dict, state: str) -> bool:
    """Owner sent a plain message while an edit is pending. True if consumed."""
    chat = msg["chat"]["id"]
    if state not in SET_PROMPTS:
        redis("DEL", "admin_state")
        return False

    if state == "qr":
        doc = msg.get("document") or {}
        if msg.get("photo"):
            file_id = msg["photo"][-1]["file_id"]
        elif str(doc.get("mime_type", "")).startswith("image/"):
            file_id = doc["file_id"]
        else:
            say(chat, "Send the QR as a photo, or /cancelpay.")
            return True
        set_cfg("qr", file_id)
        redis("DEL", "admin_state")
        say(chat, "QR updated.")
        return True

    value = (msg.get("text") or "").strip()
    if not value:
        say(chat, "Send text, or /cancelpay.")
        return True
    set_cfg(state, value)
    redis("DEL", "admin_state")
    say(chat, "Updated. Customers see it now.")
    return True


def handle_callback(cb: dict):
    """Inline button taps (owner only)."""
    cb_id = cb["id"]
    if cb.get("from", {}).get("id") != OWNER_ID:
        tg("answerCallbackQuery", callback_query_id=cb_id, text="Not allowed.", show_alert=True)
        return

    data = cb.get("data") or ""
    message = cb.get("message") or {}
    chat = message.get("chat", {}).get("id", OWNER_ID)

    if data.startswith("set:"):
        action = data.split(":", 1)[1]
        tg("answerCallbackQuery", callback_query_id=cb_id)
        if action == "show":
            say(chat, settings_text())
        elif action in SET_PROMPTS:
            redis("SET", "admin_state", action, "EX", 3600)
            say(chat, SET_PROMPTS[action] + "\nSend /cancelpay to cancel.")
        return

    if data.startswith("pay:"):
        try:
            _, action, uid = data.split(":")
            uid = int(uid)
        except ValueError:
            tg("answerCallbackQuery", callback_query_id=cb_id)
            return

        if action == "link":
            link = cfg("link")
            if not link:
                tg("answerCallbackQuery", callback_query_id=cb_id,
                   text="Set the channel link first: /pay", show_alert=True)
                return
            res = tg("sendMessage", chat_id=uid, text=f"Payment confirmed. Join here: {link}")
            status = "Link sent"
        else:
            res = tg("sendMessage", chat_id=uid, text=TXT_REJECTED)
            status = "Marked not received"
            if res.get("ok"):
                # let them send a fresh screenshot
                redis("SET", f"payflow:{uid}", "1", "EX", PAYFLOW_TTL)

        if not res.get("ok"):
            tg("answerCallbackQuery", callback_query_id=cb_id,
               text="Couldn't reach the user. They may have blocked the bot.", show_alert=True)
            return

        tg("answerCallbackQuery", callback_query_id=cb_id, text=status)
        if message.get("message_id"):
            tg("editMessageText", chat_id=chat, message_id=message["message_id"],
               text=f"{message.get('text', '')}\n\nStatus: {status}")
        return

    tg("answerCallbackQuery", callback_query_id=cb_id)


OWNER_CMDS = {
    "/start": owner_start,
    "/users": cmd_users,
    "/ban": cmd_ban,
    "/unban": cmd_unban,
    "/bans": cmd_bans,
    "/broadcast": cmd_broadcast,
    "/delete": cmd_delete,
    "/pay": cmd_pay,
    "/cancelpay": cmd_cancelpay,
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

    # Pending payment-setting edit (offer / UPI / QR / link / greeting)
    if not msg.get("reply_to_message"):
        state = redis("GET", "admin_state")
        if state and apply_admin_state(msg, state):
            return

    replied = msg.get("reply_to_message")
    if not replied:
        tg("sendMessage", chat_id=owner_chat,
           text="To respond to someone, use Telegram's Reply on their message.")
        return

    route = get_route(replied["message_id"])
    if not route:
        tg("sendMessage", chat_id=owner
