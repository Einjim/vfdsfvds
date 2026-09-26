# AI Trading Analysis Bot — Vercel zero-config

A Telegram bot where people send you a screenshot (chart, open position,
platform screen, whatever) and get it analyzed by Gemini, then can keep
asking follow-up questions about that same screenshot. Everyone gets 1 free
analysis; referring 5 more users earns +1 free (repeatable every 5
referrals). Past that, people subscribe to a plan and pay manually — card or
crypto — by sending a photo of their payment proof, which you approve or
reject from inside the bot.

This keeps the exact same deployment shape as the original "yes" bot: one
`index.py` at the project root, no `vercel.json`, Vercel's zero-config Python
builder detects the Flask `app` and routes every path through it. The
same connect page is still here — paste a bot token, it registers a webhook
— nothing else has to be typed into any config screen.

## Environment variable — just one

Add **`DATABASE_URL`** in your Vercel project's environment variables: a
Postgres connection string. That's it — this is the only thing this app
needs from the environment.

- Easiest path: in your Vercel project, go to **Storage → Create → Postgres**
  (this is Neon under the hood) and attach it to the project. Vercel usually
  exposes this as `POSTGRES_URL` rather than `DATABASE_URL` — this app checks
  for `DATABASE_URL`, `POSTGRES_URL`, `POSTGRES_PRISMA_URL`, and
  `POSTGRES_URL_NON_POOLING`, in that order, and uses whichever it finds. So
  either name works; you don't need to rename anything.
- Any other Postgres works too (Neon, Supabase, Railway, etc.) — just paste
  its connection string in as `DATABASE_URL`.
- Redeploy after adding it, since env var changes need a fresh deployment to
  take effect.

Tables are created automatically the first time they're needed
(`CREATE TABLE IF NOT EXISTS …`) — nothing to migrate by hand.

## Deploy

```bash
npm i -g vercel   # if not already installed
vercel            # from inside this folder
```

Then:
1. Open the deployment URL, paste your bot token (from @BotFather), hit
   **Connect bot**.
2. Open your bot on Telegram and send **`/claimadmin`** — the first person to
   send this becomes the bot's admin (works once; nobody else can take it
   over after that).
3. Send **`/admin`** to see the admin commands and set things up (below).

## Admin commands

Only the chat that ran `/claimadmin` can use these:

| Command | What it does |
|---|---|
| `/setprompt <text>` | Sets the system prompt Gemini uses when analyzing screenshots |
| `/setcard <text>` | Card / bank-transfer payment instructions shown to buyers |
| `/setcrypto <text>` | Crypto wallet / instructions shown to buyers |
| `/addplan Name \| Price label \| duration_days` | Adds a plan, e.g. `/addplan Monthly \| 500,000 Toman \| 30` |
| `/plans` | Lists all plans with their IDs |
| `/delplan <id>` | Deactivates a plan (hides it from buyers) |
| `/pending` | Re-lists all pending payment claims with Approve/Reject buttons |
| `/stats` | Quick counts: users, active plans, pending payments |

**No plans exist until you add at least one with `/addplan`** — until then,
the Plans button just tells users none are available yet.

## How the free / referral / plan gating works

- Every user gets **1 free screenshot analysis**, ever.
- Each user has a personal referral link (shown under 👤 My Account):
  `https://t.me/<your_bot>?start=<their_telegram_id>`. Every **5** people who
  join through it earns **+1** free analysis — this repeats (10 referrals =
  +2, 15 = +3, etc.).
- The free/plan check only happens when someone sends a **new** screenshot.
  Follow-up text questions about that same screenshot are unlimited and free
  once the screenshot itself was allowed through — so a user isn't charged
  per message, just per screenshot.
- Once someone has an active (non-expired) plan, they get unlimited
  screenshot analyses until it expires, regardless of free credits.
- Payment is fully manual: user picks a plan → picks Card or Crypto → bot
  shows your instructions → user sends a photo of their proof → it's
  forwarded to you with ✅ Approve / ❌ Reject buttons → approving activates
  the plan (and extends on top of any time they already had left, if any).

## About the hardcoded Gemini key

Per your instructions, the Gemini API key is hardcoded near the top of
`index.py` (`GEMINI_API_KEY`) instead of read from an environment variable,
since you said it's a test key. Two things worth knowing:

1. **If this ever ends up in a public GitHub repo**, anyone can read that key
   out of the source and spend your Gemini quota with it. Fine for a private
   repo / test key, but swap it for a fresh one (or move it to an env var)
   before this is a real production key.
2. Google is in the middle of migrating Gemini API keys to a new `AQ.`
   prefix format (yours is one of these), replacing the older `AIzaSy…`
   format. Most `AQ.` keys work fine with `generateContent` using the
   `x-goog-api-key` header (what this code uses), but there are scattered
   reports of some `AQ.` keys getting `401 UNAUTHENTICATED` /
   `ACCESS_TOKEN_TYPE_UNSUPPORTED` on this endpoint depending on how the
   underlying Google Cloud project is set up. If the bot always replies with
   the "⚠️ The AI service returned an error" fallback, check your Vercel
   function logs (`vercel logs`) for the actual status code Gemini returned:
   - `401` → regenerate the key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey),
     or check the linked Google Cloud project's API key settings.
   - `404` on the model name → the model in `GEMINI_MODEL` was retired;
     check the current list at [ai.google.dev/gemini-api/docs/models](https://ai.google.dev/gemini-api/docs/models)
     and swap it in.

## Files

- `index.py` — the whole app: connect page, webhook handler, Postgres
  schema + helpers, Telegram API calls, Gemini calls, all bot logic
- `requirements.txt` — Flask, requests, psycopg2-binary
- `test_flow.py`, `test_routes.py` — optional local regression tests (see
  below); not needed for deployment

## Running the tests locally (optional)

These exercise the full bot logic (referrals, free/plan gating, the
payment approve/reject flow, admin commands) against a real local Postgres,
with Telegram and Gemini network calls faked out — useful if you modify
`index.py` and want a quick sanity check.

```bash
sudo apt-get install postgresql   # or use any local Postgres you already have
sudo service postgresql start
sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'testpass';"
sudo -u postgres createdb tradingbot_test
pip install -r requirements.txt
python test_flow.py
python test_routes.py
```

## Notes

- Anyone who has your bot token can control the bot — treat it like a
  password. As before, this app doesn't store the token anywhere itself;
  it's only carried in the webhook URL Telegram calls.
- Only screenshots sent as **photos** are analyzed (the normal way Telegram
  apps send a pasted/attached image). Sending an image as a "file/document"
  instead isn't handled.
- Plans are purely time-based (X days of unlimited access) — there's no
  built-in per-message metering during an active plan.
