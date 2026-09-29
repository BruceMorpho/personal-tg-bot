# Live Demo 

https://t.me/teamxrelaybot

# Telegram Anonymous Relay Bot (Vercel)

A Telegram bot that forwards every message sent to it (text, photos, videos, voice, documents, stickers...)
to **your** private chat. When you **reply** to a forwarded message, the bot sends your reply to the
original sender. The reply comes from the bot, so your name, username and ID are never revealed.

## How it works
- Visitor messages the bot -> bot sends you a header (name, username, ID) + a copy of the message.
- You use Telegram's **Reply** on the header or the copied message.
- The bot copies your reply back to the visitor (no "forwarded from" tag).
- Runs on Vercel as a serverless **webhook**. Reply routes are stored in **Upstash Redis** (90 days).

## Project structure
```
relaybot-vercel/
├── api/index.py       # the bot (Flask webhook)
├── requirements.txt   # flask, httpx
├── vercel.json        # routes every request to api/index.py
├── .env.example       # variables you must set on Vercel
├── .gitignore
└── README.md
```

## Requirements
| Need | Notes |
|---|---|
| Telegram account | to create the bot and receive messages |
| Bot token | from @BotFather (`/newbot`) |
| Your numeric Telegram ID | from @userinfobot |
| GitHub account | to host the code |
| Vercel account | free Hobby plan is enough |
| Upstash Redis | free, added from Vercel Storage |
| Python packages | `flask`, `httpx` (installed by Vercel from requirements.txt) |

## Full deployment guide

### 1. Create the bot
1. Open **@BotFather** -> `/newbot` -> follow the steps -> copy the **token**.
2. Open **@userinfobot** -> copy your numeric **ID**.
3. Open your new bot and press **Start** once (so it is allowed to message you).

### 2. Push the code to GitHub
```bash
cd relaybot-vercel
git init
git add .
git commit -m "relay bot"
git branch -M main
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```
Keep the folder structure exactly as is (`api/index.py` must be inside `api/`).

### 3. Import into Vercel
1. Go to https://vercel.com/new and import your repo.
2. Framework preset: **Other** (or Flask if auto-detected).
3. Do **not** deploy yet.

### 4. Add Redis (Upstash)
Project -> **Storage** -> **Create Database** -> **Upstash (Redis)** -> connect it to the project.
(Do NOT pick "Edge Config" - it is read-only and will not work.)
Vercel adds `KV_REST_API_URL` and `KV_REST_API_TOKEN` automatically.

### 5. Add environment variables
Project -> Settings -> Environment Variables (Production + Preview):

| Name | Value |
|---|---|
| `BOT_TOKEN` | token from BotFather |
| `OWNER_ID` | your numeric ID |
| `WEBHOOK_SECRET` | any random string (`openssl rand -hex 24`) |

### 6. Deploy
Click **Deploy**. Open your production URL, e.g. `https://YOUR-PROJECT.vercel.app`.
It should show: `relay bot is running`.

(If you change env variables later, you must **Redeploy**.)

### 7. Turn off Vercel Authentication
Telegram cannot log in to Vercel, so protected URLs return **401** to it.
Project -> Settings -> **Deployment Protection** -> set **Vercel Authentication** to **Disabled** -> Save.
The bot is still safe: it rejects any request that does not carry your `WEBHOOK_SECRET`.

**If you want to keep protection on**, create an *Automation Bypass* secret (Deployment Protection ->
Protection Bypass for Automation) and use it in the webhook URL (see step 8, option B).

### 8. Set the webhook
Open in a browser (replace the placeholders):

**Option A - protection disabled**
```
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://YOUR-PROJECT.vercel.app&secret_token=<WEBHOOK_SECRET>
```

**Option B - protection still on (use bypass secret)**
```
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://YOUR-PROJECT.vercel.app/%3Fx-vercel-protection-bypass%3D<BYPASS_SECRET>&secret_token=<WEBHOOK_SECRET>
```

Expected: `{"ok":true,"result":true,"description":"Webhook was set"}`
Add `&drop_pending_updates=true` to clear old stuck updates.

### 9. Test
1. Send `/start` to the bot from your account -> "Relay is active".
2. From a **second** Telegram account, send text/photo/video to the bot.
3. It appears in your chat with a header. **Reply** to it.
4. The second account receives your reply from the bot, without your details.

## Troubleshooting
Check status: `https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo`

| Symptom | Cause / fix |
|---|---|
| `last_error_message: 401 Unauthorized` | Vercel Authentication is on. Disable it (step 7) or use the bypass URL (step 8B). |
| `last_error_message: 403` / bot ignores messages | `secret_token` in setWebhook does not match `WEBHOOK_SECRET` on Vercel. |
| `pending_update_count` keeps growing | Telegram cannot reach the webhook - see errors above. |
| `/start` works but replies fail | Redis not connected (`KV_REST_API_URL` missing). Connect Upstash, then Redeploy. |
| "I can't tell who that message came from" | You replied to a message that is not a relayed one, or the route is older than 90 days. |
| 500 errors after editing env vars | Redeploy. Make sure `BOT_TOKEN`, `OWNER_ID`, `WEBHOOK_SECRET` all exist. |
| Bot never messages you | You must press **Start** on your own bot once. |

## Security notes
- Never commit or share your bot token. If it leaks: @BotFather -> `/revoke`, update `BOT_TOKEN` on Vercel,
  Redeploy, and run setWebhook again with the new token.
- Keep your bypass secret (if used) private - it is inside the webhook URL.
- Only one bot instance per token: do not run a polling script at the same time as the webhook.

## Limitations
- Photo albums arrive as separate messages; reply to each one.
- Only private chats are handled (groups are ignored).
- Users who block the bot cannot receive replies (you get an error message).
