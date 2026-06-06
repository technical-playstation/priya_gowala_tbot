# PriyaBot — Production Telegram AI Companion

A fully production-ready Telegram SaaS bot with Gemini AI, Supabase, UPI payments, and voice notes. Deploys to Render.com in minutes.

---

## Features

- 🤖 **Gemini AI** — multilingual (Hindi, Hinglish, Bengali, English)
- 💬 **Conversation memory** — persistent per-user history
- 🎙️ **Premium voice notes** — edge-tts with auto language detection
- 💳 **UPI payments** — dynamic QR, UTR verification, admin approval
- 📦 **3 subscription plans** — ₹11/₹29/₹49
- 🛡️ **Rate limiting** — per-user sliding window
- 📊 **Structured logging** — separate log files per module
- 🔒 **Security hardened** — all secrets in env vars, webhook token verification
- ☁️ **Render-ready** — webhook, Gunicorn, health endpoint

---

## Project Structure

```
project/
├── bot.py              # Telegram handlers, webhook, Flask app
├── ai_engine.py        # Gemini integration with retry + memory
├── database.py         # Supabase ORM layer
├── payment.py          # UPI QR + UTR workflow
├── voice_engine.py     # edge-tts voice generation
├── config.py           # Centralised settings + env validation
├── logger.py           # Structured rotating file loggers
├── schema.sql          # Supabase table definitions
├── requirements.txt
├── runtime.txt         # python-3.11.9
├── render.yaml         # Render deployment config
├── .env.example        # Environment variable template
├── logs/               # Auto-created log files
├── temp/               # Auto-created voice temp files
└── utils/
    ├── rate_limiter.py
    └── helpers.py
```

---

## Quick Start

### 1. Clone & install locally

```bash
git clone https://github.com/YOUR_USERNAME/priyabot.git
cd priyabot
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your credentials
python bot.py
```

---

## Supabase Setup

1. Go to [app.supabase.com](https://app.supabase.com) → **New Project**
2. Note your **Project URL** and **service_role key** (Settings → API)
3. Open **SQL Editor** → **New Query** → paste contents of `schema.sql` → **Run**
4. All 5 tables are created with indexes and RLS enabled

---

## Telegram Bot Setup

1. Open [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the **Bot Token**
3. Set commands (optional but nice):
   ```
   /setcommands → your_bot
   start - Start chatting
   help - What I can do
   status - Your account
   subscription - Plans & subscribe
   ```

---

## Google Gemini API Key

1. Go to [aistudio.google.com](https://aistudio.google.com/app/apikey)
2. Click **Create API Key**
3. Copy the key

---

## Find Your Admin Chat ID

Send `/start` to [@userinfobot](https://t.me/userinfobot) — it returns your numeric chat ID.

---

## Render Deployment

### Step 1 — Push to GitHub

```bash
git add .
git commit -m "Initial deploy"
git push origin main
```

### Step 2 — Create Render Web Service

1. Go to [dashboard.render.com](https://dashboard.render.com) → **New → Web Service**
2. Connect your GitHub repo
3. Render auto-detects `render.yaml` — just confirm

### Step 3 — Set Environment Variables

In Render dashboard → **Environment**:

| Key | Value |
|-----|-------|
| `TELEGRAM_BOT_TOKEN` | Your bot token |
| `GEMINI_API_KEY` | Your Gemini key |
| `SUPABASE_URL` | `https://xxxx.supabase.co` |
| `SUPABASE_KEY` | Your service_role key |
| `ADMIN_CHAT_ID` | Your numeric Telegram chat ID |
| `UPI_ID` | e.g. `yourname@upi` |
| `RENDER_APP_NAME` | The Render service name (e.g. `priyabot`) |
| `WEBHOOK_SECRET` | Any random string (optional but recommended) |

### Step 4 — Deploy

Click **Manual Deploy → Deploy Latest Commit**. Render will:
1. Install dependencies
2. Run the webhook registration command
3. Start Gunicorn
4. Health check at `/health`

### Step 5 — Verify

- Visit `https://YOUR_APP.onrender.com/health` — should return `{"status":"ok"}`
- Send `/start` to your bot on Telegram

---

## Payment Workflow

1. User runs `/subscription` → picks a plan
2. Bot sends a UPI QR image with instructions
3. User pays via any UPI app, copies the **UTR / Transaction ID**
4. User replies: `utr:SESSION_ID:THEIR_UTR`
5. Admin receives notification; runs `/admin` to review
6. Admin taps ✅ Approve → subscription activates immediately
7. Both admin and user are notified

---

## Subscription Plans

| Key | Price | Duration | Description |
|-----|-------|----------|-------------|
| `basic` | ₹11 | 3 days | Trial plan |
| `monthly` | ₹29 | 30 days | First purchase |
| `standard` | ₹49 | 30 days | Standard plan |

Edit `PLANS` in `config.py` to change prices or durations.

---

## Voice Notes (Premium)

Subscribers can prefix any message with `voice:` to receive an audio reply:

```
voice: Kya haal hai aaj?
```

The bot auto-detects Hindi / Hinglish / Bengali / English and uses the appropriate neural voice.

---

## Rate Limiting

- **20 messages per 60 seconds** per user (configurable in `config.py`)
- In-memory sliding window — resets on bot restart
- Spammers get a friendly warning

---

## Logs

Log files are created automatically in `logs/`:

| File | Contents |
|------|----------|
| `app.log` | General application events |
| `bot.log` | Bot handler events |
| `ai.log` | Gemini requests and responses |
| `voice.log` | Voice generation |
| `payment.log` | Payment events |
| `database.log` | DB operations and errors |
| `admin.log` | Admin actions |

Logs rotate at 5 MB, keeping 5 backups each.

On Render, a persistent disk is mounted at `/app/logs` (configured in `render.yaml`).

---

## Troubleshooting

**Bot not responding after deploy**
- Check `RENDER_APP_NAME` matches your Render service name exactly
- Visit `/health` to confirm the server is up
- Check Render logs for startup errors

**Webhook not registered**
- Verify `TELEGRAM_BOT_TOKEN` is correct
- Check `RENDER_APP_NAME` is set
- Manually register: `curl "https://api.telegram.org/botTOKEN/setWebhook?url=https://YOUR_APP.onrender.com/webhook"`

**Supabase errors**
- Ensure `schema.sql` was run successfully
- Use the **service_role** key, not the anon key
- Check Supabase project isn't paused (free tier pauses after 1 week of inactivity)

**Voice not working**
- Voice notes require an active subscription
- Check `edge-tts` is installed correctly
- Check `temp/` directory exists and is writable

**Payment not approved**
- Admin must run `/admin` to see pending payments
- The admin's chat ID must match `ADMIN_CHAT_ID` exactly

---

## Security Notes

- Never commit `.env` — it's in `.gitignore`
- Use Render environment variables for all secrets
- The `SUPABASE_KEY` used should be the `service_role` key (server-side only)
- `WEBHOOK_SECRET` adds an extra layer of request verification
- All UTRs are deduplicated at the database level to prevent double-activation

---

## License

MIT
