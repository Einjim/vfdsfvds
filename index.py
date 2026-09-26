"""
AI Trading-Analysis Telegram Bot — single-file Flask app for Vercel's
zero-config Python builder (same deployment style as the original
"yes" bot: no vercel.json, index.py exposes `app`, every path is routed
through it).

WHAT THIS DOES
- Web page at "/" : paste a bot token, it verifies it with Telegram and
  registers a webhook — exactly like before. The token itself is never
  stored anywhere; it's only carried in the webhook URL
  (/api/webhook/<token>), so every request tells us which bot it's for.
- Webhook at "/api/webhook/<token>": handles all bot logic.
    * Users send a chart/position screenshot -> it's sent to Gemini
      (vision) with a configurable system prompt, and the answer is
      sent back. Users can then keep asking follow-up questions about
      that same screenshot.
    * Every user gets 1 free analysis. Referring 5 more users (via
      their personal /start link) grants +1 free analysis, repeatable
      every 5 referrals.
    * Beyond that, users pick a plan and pay manually (card or
      crypto) — they send a photo of their payment proof, which is
      forwarded to the admin's chat with Approve/Reject buttons.
    * All tables are created automatically on first use
      (CREATE TABLE IF NOT EXISTS), so there is nothing to migrate by
      hand.

ENVIRONMENT VARIABLES
- DATABASE_URL : a Postgres connection string (Vercel Postgres / Neon /
  Supabase / any Postgres works). This is the ONLY environment
  variable this app needs. If you use Vercel's own Postgres storage
  integration, it usually exposes POSTGRES_URL instead — this app
  looks for either name automatically (see get_db_url() below).

BECOMING THE BOT'S ADMIN
- After connecting your bot on the web page, open a chat with your bot
  on Telegram and send:  /claimadmin
  The first person to do this becomes the admin (only works once).
  Then send /admin to see the admin command list (set the AI system
  prompt, payment instructions, plans, review pending payments, etc).

NOTE ON THE HARDCODED GEMINI KEY
- Per your instructions this is hardcoded below instead of read from
  an environment variable, since you said it's a test key. Just keep
  in mind that if this code ends up in a public GitHub repo, anyone
  can read it and spend your Gemini quota — swap it out (or move it to
  an env var) whenever this stops being just a test.
"""

import base64
import re
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, request, render_template_string

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Hardcoded config (per your request — only DATABASE_URL goes in env vars)
# ---------------------------------------------------------------------------

GEMINI_API_KEY = "AQ.Ab8RN6I6LkkfCv9deZA8O0yh0c7D99KVQciJ68Fv5Vra_fMINw"
GEMINI_API_KEY = "AQ.Ab8RN6Loa_ybBHj6_ovrjoogsAq9N2wOauc7SLEV_Q_VjSbQjw"
GEMINI_MODEL = "gemini-3.5-flash"  # change here if you want e.g. gemini-3.6-flash
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

FREE_ANALYSES_BASE = 1000
REFERRALS_PER_BONUS_CREDIT = 5

DEFAULT_SYSTEM_PROMPT = (
    "You are a knowledgeable trading and markets analysis assistant embedded "
    "in a Telegram bot. Users send you screenshots of charts, trading "
    "positions, or platform screens and ask questions about them. Carefully "
    "look at the image and answer based on what is actually visible in it: "
    "price action, indicators, position size, entry/exit levels, P&L, order "
    "book, etc. Be specific and reference what you see. If the image is "
    "unclear or unrelated to trading, say so honestly instead of guessing. "
    "Keep answers concise and practical. Always make clear that your "
    "analysis is informational only, not financial advice, and that the "
    "user is responsible for their own trading decisions."
)

DEFAULT_QUESTION = "Analyze this screenshot and tell me what stands out."

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_FILE_API = "https://api.telegram.org/file/bot{token}/{file_path}"

# A Telegram bot token always looks like  <digits>:<35 alnum/_- chars>
# The digits before the colon are the bot's own Telegram user id, so we
# can pull that out of the URL with no extra API call.
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")

BTN_NEW = "📊 New Analysis"
BTN_PLANS = "💳 Plans"
BTN_ACCOUNT = "👤 My Account"
BTN_HELP = "ℹ️ Help"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bots (
    bot_id BIGINT PRIMARY KEY,
    username TEXT,
    admin_chat_id BIGINT,
    system_prompt TEXT,
    pay_card_info TEXT,
    pay_crypto_info TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    telegram_id BIGINT NOT NULL,
    username TEXT,
    first_name TEXT,
    free_used_count INT NOT NULL DEFAULT 0,
    active_plan_id INT,
    plan_expires_at TIMESTAMPTZ,
    active_image_file_id TEXT,
    active_image_set_at TIMESTAMPTZ,
    pending_payment_plan_id INT,
    pending_payment_method TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bot_id, telegram_id)
);

CREATE TABLE IF NOT EXISTS referrals (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    referrer_telegram_id BIGINT NOT NULL,
    referred_telegram_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bot_id, referred_telegram_id)
);

CREATE TABLE IF NOT EXISTS plans (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    price_label TEXT NOT NULL,
    duration_days INT NOT NULL DEFAULT 30,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    user_telegram_id BIGINT NOT NULL,
    plan_id INT NOT NULL,
    method TEXT NOT NULL,
    proof_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS messages_log (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    user_telegram_id BIGINT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_log_user
    ON messages_log (bot_id, user_telegram_id, created_at);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer
    ON referrals (bot_id, referrer_telegram_id);
"""


def get_db_url():
    import os

    for name in ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL", "POSTGRES_URL_NON_POOLING"):
        v = os.environ.get(name)
        if v:
            return v
    return None


def get_conn():
    url = get_db_url()
    if not url:
        raise RuntimeError(
            "No database configured. Add a DATABASE_URL environment variable "
            "in your Vercel project settings and redeploy."
        )
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)


def ensure_bot_row(conn, bot_id, username=None):
    with conn.cursor() as cur:
        if username:
            cur.execute(
                "INSERT INTO bots (bot_id, username) VALUES (%s, %s) "
                "ON CONFLICT (bot_id) DO UPDATE SET username = EXCLUDED.username",
                (bot_id, username),
            )
        else:
            cur.execute(
                "INSERT INTO bots (bot_id) VALUES (%s) ON CONFLICT (bot_id) DO NOTHING",
                (bot_id,),
            )


def get_bot_row(conn, bot_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM bots WHERE bot_id = %s", (bot_id,))
        return cur.fetchone()


def is_admin_chat(conn, bot_id, chat_id):
    bot = get_bot_row(conn, bot_id)
    return bool(bot and bot["admin_chat_id"] == chat_id)


def get_or_create_user(conn, bot_id, chat):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM users WHERE bot_id = %s AND telegram_id = %s",
            (bot_id, chat["id"]),
        )
        row = cur.fetchone()
        if row:
            return row, False
        cur.execute(
            "INSERT INTO users (bot_id, telegram_id, username, first_name) "
            "VALUES (%s, %s, %s, %s) RETURNING *",
            (bot_id, chat["id"], chat.get("username"), chat.get("first_name")),
        )
        return cur.fetchone(), True


def register_referral(conn, bot_id, referrer_id, referred_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO referrals (bot_id, referrer_telegram_id, referred_telegram_id) "
            "VALUES (%s, %s, %s) ON CONFLICT (bot_id, referred_telegram_id) DO NOTHING",
            (bot_id, referrer_id, referred_id),
        )


def get_referral_count(conn, bot_id, telegram_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS c FROM referrals WHERE bot_id = %s AND referrer_telegram_id = %s",
            (bot_id, telegram_id),
        )
        return cur.fetchone()["c"]


def check_entitlement(conn, bot_id, user_row):
    """Can this user start a NEW screenshot analysis right now?"""
    now = datetime.now(timezone.utc)
    if user_row["plan_expires_at"] and user_row["plan_expires_at"] > now:
        return True, "plan"
    ref_count = get_referral_count(conn, bot_id, user_row["telegram_id"])
    free_total = FREE_ANALYSES_BASE + (ref_count // REFERRALS_PER_BONUS_CREDIT)
    if user_row["free_used_count"] < free_total:
        return True, "free"
    return False, None


def increment_free_used(conn, bot_id, chat_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET free_used_count = free_used_count + 1 "
            "WHERE bot_id = %s AND telegram_id = %s",
            (bot_id, chat_id),
        )


def set_active_image(conn, bot_id, chat_id, file_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET active_image_file_id = %s, active_image_set_at = now() "
            "WHERE bot_id = %s AND telegram_id = %s",
            (file_id, bot_id, chat_id),
        )


def clear_active_image(conn, bot_id, chat_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET active_image_file_id = NULL, active_image_set_at = NULL "
            "WHERE bot_id = %s AND telegram_id = %s",
            (bot_id, chat_id),
        )


def set_pending_payment(conn, bot_id, chat_id, plan_id, method):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET pending_payment_plan_id = %s, pending_payment_method = %s "
            "WHERE bot_id = %s AND telegram_id = %s",
            (plan_id, method, bot_id, chat_id),
        )


def log_message(conn, bot_id, chat_id, role, content):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages_log (bot_id, user_telegram_id, role, content) "
            "VALUES (%s, %s, %s, %s)",
            (bot_id, chat_id, role, content),
        )


def get_recent_history(conn, bot_id, telegram_id, since, limit=6):
    if not since:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role, content FROM messages_log "
            "WHERE bot_id = %s AND user_telegram_id = %s AND created_at >= %s "
            "ORDER BY id DESC LIMIT %s",
            (bot_id, telegram_id, since, limit),
        )
        rows = cur.fetchall()
    return list(reversed(rows))


def get_system_prompt(conn, bot_id):
    bot = get_bot_row(conn, bot_id)
    return (bot and bot["system_prompt"]) or DEFAULT_SYSTEM_PROMPT


def get_plan(conn, bot_id, plan_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM plans WHERE id = %s AND bot_id = %s", (plan_id, bot_id))
        return cur.fetchone()


def list_active_plans(conn, bot_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM plans WHERE bot_id = %s AND active = TRUE ORDER BY duration_days",
            (bot_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def tg_call(token, method, **params):
    """Call a Telegram Bot API method and return the parsed JSON response."""
    url = TELEGRAM_API.format(token=token, method=method)
    resp = requests.post(url, json=params, timeout=15)
    return resp.json()


def tg_send_message(token, chat_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    try:
        return tg_call(token, "sendMessage", **params)
    except requests.RequestException as e:
        print("sendMessage failed:", e)
        return None


def tg_send_photo(token, chat_id, file_id, caption=None, reply_markup=None):
    params = {"chat_id": chat_id, "photo": file_id}
    if caption:
        params["caption"] = caption
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    try:
        return tg_call(token, "sendPhoto", **params)
    except requests.RequestException as e:
        print("sendPhoto failed:", e)
        return None


def tg_edit_message_caption(token, chat_id, message_id, caption, reply_markup=None):
    params = {"chat_id": chat_id, "message_id": message_id, "caption": caption}
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    try:
        return tg_call(token, "editMessageCaption", **params)
    except requests.RequestException as e:
        print("editMessageCaption failed:", e)
        return None


def tg_edit_message_text(token, chat_id, message_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    try:
        return tg_call(token, "editMessageText", **params)
    except requests.RequestException as e:
        print("editMessageText failed:", e)
        return None


def tg_answer_callback(token, callback_query_id, text=None, show_alert=False):
    params = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        params["text"] = text
    try:
        return tg_call(token, "answerCallbackQuery", **params)
    except requests.RequestException as e:
        print("answerCallbackQuery failed:", e)
        return None


def tg_get_file_bytes(token, file_id):
    info = tg_call(token, "getFile", file_id=file_id)
    file_path = info["result"]["file_path"]
    url = TELEGRAM_FILE_API.format(token=token, file_path=file_path)
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    return r.content


def main_menu_kb():
    return {
        "keyboard": [[BTN_NEW], [BTN_PLANS, BTN_ACCOUNT], [BTN_HELP]],
        "resize_keyboard": True,
    }


def plans_inline_kb(plans):
    rows = [
        [{"text": f"{p['name']} — {p['price_label']}", "callback_data": f"plan:{p['id']}"}]
        for p in plans
    ]
    return {"inline_keyboard": rows}


def payment_method_kb(plan_id):
    return {
        "inline_keyboard": [[
            {"text": "💳 Card", "callback_data": f"pay:card:{plan_id}"},
            {"text": "🪙 Crypto", "callback_data": f"pay:crypto:{plan_id}"},
        ]]
    }


def approve_reject_kb(payment_id):
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"payapprove:{payment_id}"},
            {"text": "❌ Reject", "callback_data": f"payreject:{payment_id}"},
        ]]
    }


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def gemini_ask(system_prompt, image_bytes, mime_type, question, history):
    contents = []
    for h in history:
        contents.append({"role": h["role"], "parts": [{"text": h["content"] or ""}]})

    parts = []
    if image_bytes:
        parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": base64.b64encode(image_bytes).decode("ascii"),
            }
        })
    parts.append({"text": question})
    contents.append({"role": "user", "parts": parts})

    body = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_prompt}]},
    }
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}

    try:
        resp = requests.post(GEMINI_URL, headers=headers, json=body, timeout=55)
    except requests.RequestException as e:
        print("Gemini request failed:", e)
        return "⚠️ Couldn't reach the AI service right now. Please try again in a moment."

    try:
        data = resp.json()
    except ValueError:
        print("Gemini returned non-JSON:", resp.status_code, resp.text[:500])
        return "⚠️ The AI service returned an unexpected response. Please try again."

    if resp.status_code != 200:
        print("Gemini error", resp.status_code, data)
        return "⚠️ The AI service returned an error. Please try again in a moment."

    candidates = data.get("candidates") or []
    if not candidates:
        reason = data.get("promptFeedback", {}).get("blockReason", "unknown")
        return f"⚠️ The AI couldn't analyze this image (reason: {reason}). Try a clearer or different screenshot."

    text_parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in text_parts).strip()
    return text or "⚠️ The AI didn't return any text for this image. Please try again."


# ---------------------------------------------------------------------------
# Bot logic — commands & menus
# ---------------------------------------------------------------------------

def handle_start(conn, token, bot_id, chat, is_new, payload):
    if is_new and payload:
        try:
            ref_id = int(payload)
            if ref_id != chat["id"]:
                register_referral(conn, bot_id, ref_id, chat["id"])
        except ValueError:
            pass
    tg_send_message(
        token,
        chat["id"],
        "👋 Welcome! Send me a screenshot of a chart or trading position and "
        "I'll analyze it for you.\n\n"
        "🆓 You get 1 free analysis. Open 👤 My Account for your referral link "
        "— every 5 friends who join through it earns you +1 free analysis.",
        reply_markup=main_menu_kb(),
    )


def claim_admin(conn, token, bot_id, chat):
    bot = get_bot_row(conn, bot_id)
    current = bot["admin_chat_id"] if bot else None
    if current is None:
        with conn.cursor() as cur:
            cur.execute("UPDATE bots SET admin_chat_id = %s WHERE bot_id = %s", (chat["id"], bot_id))
        tg_send_message(token, chat["id"], "✅ You are now the admin of this bot. Send /admin to see admin commands.")
    elif current == chat["id"]:
        tg_send_message(token, chat["id"], "You're already the admin. Send /admin to see admin commands.")
    else:
        tg_send_message(token, chat["id"], "This bot already has an admin.")


def send_help(conn, token, bot_id, chat_id):
    text = (
        "📊 Send me a screenshot of a chart or trading position and I'll analyze it. "
        "After that, keep asking follow-up questions about that same screenshot any time.\n\n"
        "🆓 You get 1 free analysis. Refer 5 friends (see 👤 My Account for your link) for +1 free "
        "— repeatable every 5 referrals.\n\n"
        "💳 Need more? Use Plans to subscribe. Payment is manual (card or crypto): pick a plan, "
        "send a photo of your payment proof, and it's activated once approved.\n\n"
        "Buttons:\n"
        f"{BTN_NEW} — start over with a new screenshot\n"
        f"{BTN_PLANS} — see subscription options\n"
        f"{BTN_ACCOUNT} — your plan & referral status"
    )
    if is_admin_chat(conn, bot_id, chat_id):
        text += "\n\n🛠 You're the admin. Send /admin for admin commands."
    tg_send_message(token, chat_id, text, reply_markup=main_menu_kb())


def send_admin_menu(token, chat_id):
    text = (
        "🛠 Admin commands:\n"
        "/setprompt <text> — set the AI's system prompt\n"
        "/setcard <text> — set card payment instructions\n"
        "/setcrypto <text> — set crypto payment instructions\n"
        "/addplan Name | Price label | duration_days — add a plan\n"
        "  example: /addplan Monthly | 500,000 Toman | 30\n"
        "/plans — list all plans (with IDs, for /delplan)\n"
        "/delplan <id> — deactivate a plan\n"
        "/pending — list pending payments for approval\n"
        "/stats — quick usage stats"
    )
    tg_send_message(token, chat_id, text)


def send_plans(conn, token, bot_id, chat_id):
    plans = list_active_plans(conn, bot_id)
    if not plans:
        tg_send_message(token, chat_id, "No plans are available yet — please check back soon.")
        return
    tg_send_message(token, chat_id, "Choose a plan:", reply_markup=plans_inline_kb(plans))


def send_account(conn, token, bot_id, chat, user_row):
    ref_count = get_referral_count(conn, bot_id, chat["id"])
    free_total = FREE_ANALYSES_BASE + (ref_count // REFERRALS_PER_BONUS_CREDIT)
    free_left = max(0, free_total - user_row["free_used_count"])
    now = datetime.now(timezone.utc)

    plan_line = "🚫 No active plan"
    if user_row["plan_expires_at"] and user_row["plan_expires_at"] > now:
        plan = get_plan(conn, bot_id, user_row["active_plan_id"]) if user_row["active_plan_id"] else None
        pname = plan["name"] if plan else "Plan"
        plan_line = f"✅ {pname} active until {user_row['plan_expires_at'].strftime('%Y-%m-%d %H:%M UTC')}"

    bot = get_bot_row(conn, bot_id)
    username = bot["username"] if bot else None
    ref_link = f"https://t.me/{username}?start={chat['id']}" if username else "(share your numeric id: {})".format(chat["id"])

    text = (
        f"{plan_line}\n"
        f"🆓 Free analyses left: {free_left} (base 1 + 1 per {REFERRALS_PER_BONUS_CREDIT} referrals)\n"
        f"👥 Referrals: {ref_count}\n"
        f"🔗 Your referral link:\n{ref_link}"
    )
    tg_send_message(token, chat["id"], text)


def show_payment_method_choice(conn, token, bot_id, chat_id, plan_id):
    plan = get_plan(conn, bot_id, plan_id)
    if not plan:
        tg_send_message(token, chat_id, "That plan is no longer available.")
        return
    tg_send_message(
        token, chat_id,
        f"How would you like to pay for {plan['name']} ({plan['price_label']})?",
        reply_markup=payment_method_kb(plan_id),
    )


def show_payment_instructions(conn, token, bot_id, chat_id, plan_id, method):
    bot = get_bot_row(conn, bot_id)
    info = None
    if bot:
        info = bot["pay_card_info"] if method == "card" else bot["pay_crypto_info"]
    if not info:
        info = "Payment details haven't been set up yet — please contact the bot admin."
    tg_send_message(
        token, chat_id,
        f"{info}\n\nAfter paying, send a photo of your payment proof here — it'll be reviewed shortly.",
    )


def create_payment_and_notify(conn, token, bot_id, chat, user_row, proof_file_id):
    plan_id = user_row["pending_payment_plan_id"]
    method = user_row["pending_payment_method"]
    plan = get_plan(conn, bot_id, plan_id) if plan_id else None

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO payments (bot_id, user_telegram_id, plan_id, method, proof_file_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (bot_id, chat["id"], plan_id, method, proof_file_id),
        )
        payment_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE users SET pending_payment_plan_id = NULL, pending_payment_method = NULL "
            "WHERE bot_id = %s AND telegram_id = %s",
            (bot_id, chat["id"]),
        )

    tg_send_message(token, chat["id"], "✅ Your payment proof was submitted and is awaiting review.")

    bot = get_bot_row(conn, bot_id)
    admin_id = bot["admin_chat_id"] if bot else None
    if admin_id:
        caption = (
            f"💰 New payment claim (#{payment_id})\n"
            f"User: {chat.get('first_name', '')} (@{chat.get('username', '-')}, id {chat['id']})\n"
            f"Plan: {plan['name'] if plan else '?'} — {plan['price_label'] if plan else '?'}\n"
            f"Method: {method}"
        )
        tg_send_photo(token, admin_id, proof_file_id, caption=caption, reply_markup=approve_reject_kb(payment_id))


def handle_payment_decision(conn, token, bot_id, payment_id, approve, admin_chat_id, admin_message_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM payments WHERE id = %s AND bot_id = %s", (payment_id, bot_id))
        payment = cur.fetchone()
        if not payment or payment["status"] != "pending":
            tg_answer_callback(token, "", "Already handled.")  # no-op safe guard
            return

        new_status = "approved" if approve else "rejected"
        cur.execute("UPDATE payments SET status = %s, reviewed_at = now() WHERE id = %s", (new_status, payment_id))

        plan = None
        if approve:
            plan = get_plan(conn, bot_id, payment["plan_id"])
            cur.execute(
                "SELECT * FROM users WHERE bot_id = %s AND telegram_id = %s",
                (bot_id, payment["user_telegram_id"]),
            )
            u = cur.fetchone()
            now = datetime.now(timezone.utc)
            base = u["plan_expires_at"] if (u and u["plan_expires_at"] and u["plan_expires_at"] > now) else now
            new_expiry = base + timedelta(days=plan["duration_days"] if plan else 30)
            cur.execute(
                "UPDATE users SET active_plan_id = %s, plan_expires_at = %s "
                "WHERE bot_id = %s AND telegram_id = %s",
                (payment["plan_id"], new_expiry, bot_id, payment["user_telegram_id"]),
            )

    if approve:
        tg_send_message(token, payment["user_telegram_id"], "🎉 Your payment was approved and your plan is now active!")
        tg_edit_message_caption(token, admin_chat_id, admin_message_id, "✅ Approved")
    else:
        tg_send_message(token, payment["user_telegram_id"], "Your payment proof was not approved. Please contact the bot admin, or try again.")
        tg_edit_message_caption(token, admin_chat_id, admin_message_id, "❌ Rejected")


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------

def cmd_setprompt(conn, token, bot_id, chat_id, arg):
    if not arg.strip():
        tg_send_message(token, chat_id, "Usage: /setprompt <new system prompt text>")
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE bots SET system_prompt = %s WHERE bot_id = %s", (arg.strip(), bot_id))
    tg_send_message(token, chat_id, "✅ System prompt updated.")


def cmd_setcard(conn, token, bot_id, chat_id, arg):
    if not arg.strip():
        tg_send_message(token, chat_id, "Usage: /setcard <card / bank transfer instructions>")
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE bots SET pay_card_info = %s WHERE bot_id = %s", (arg.strip(), bot_id))
    tg_send_message(token, chat_id, "✅ Card payment instructions updated.")


def cmd_setcrypto(conn, token, bot_id, chat_id, arg):
    if not arg.strip():
        tg_send_message(token, chat_id, "Usage: /setcrypto <wallet address / instructions>")
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE bots SET pay_crypto_info = %s WHERE bot_id = %s", (arg.strip(), bot_id))
    tg_send_message(token, chat_id, "✅ Crypto payment instructions updated.")


def cmd_addplan(conn, token, bot_id, chat_id, arg):
    parts = [p.strip() for p in arg.split("|")]
    if len(parts) != 3 or not all(parts):
        tg_send_message(
            token, chat_id,
            "Usage: /addplan Name | Price label | duration_days\n"
            "Example: /addplan Monthly | 500,000 Toman | 30",
        )
        return
    name, price_label, duration_str = parts
    try:
        duration_days = int(duration_str)
    except ValueError:
        tg_send_message(token, chat_id, "Duration must be a whole number of days.")
        return
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO plans (bot_id, name, price_label, duration_days) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (bot_id, name, price_label, duration_days),
        )
        plan_id = cur.fetchone()["id"]
    tg_send_message(token, chat_id, f"✅ Plan added (#{plan_id}): {name} — {price_label} — {duration_days} days")


def cmd_listplans_admin(conn, token, bot_id, chat_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM plans WHERE bot_id = %s ORDER BY id", (bot_id,))
        rows = cur.fetchall()
    if not rows:
        tg_send_message(token, chat_id, "No plans yet. Add one with /addplan.")
        return
    lines = [
        f"#{p['id']} {'✅' if p['active'] else '🚫'} {p['name']} — {p['price_label']} — {p['duration_days']}d"
        for p in rows
    ]
    tg_send_message(token, chat_id, "\n".join(lines))


def cmd_delplan(conn, token, bot_id, chat_id, arg):
    try:
        plan_id = int(arg.strip())
    except ValueError:
        tg_send_message(token, chat_id, "Usage: /delplan <plan id>")
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE plans SET active = FALSE WHERE id = %s AND bot_id = %s", (plan_id, bot_id))
    tg_send_message(token, chat_id, f"Plan #{plan_id} deactivated.")


def cmd_pending(conn, token, bot_id, chat_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM payments WHERE bot_id = %s AND status = 'pending' ORDER BY id", (bot_id,))
        rows = cur.fetchall()
    if not rows:
        tg_send_message(token, chat_id, "No pending payments.")
        return
    for p in rows:
        plan = get_plan(conn, bot_id, p["plan_id"])
        caption = f"#{p['id']} user {p['user_telegram_id']} — {plan['name'] if plan else '?'} ({p['method']})"
        if p["proof_file_id"]:
            tg_send_photo(token, chat_id, p["proof_file_id"], caption=caption, reply_markup=approve_reject_kb(p["id"]))
        else:
            tg_send_message(token, chat_id, caption, reply_markup=approve_reject_kb(p["id"]))


def cmd_stats(conn, token, bot_id, chat_id):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) c FROM users WHERE bot_id = %s", (bot_id,))
        users_c = cur.fetchone()["c"]
        cur.execute("SELECT count(*) c FROM users WHERE bot_id = %s AND plan_expires_at > now()", (bot_id,))
        active_c = cur.fetchone()["c"]
        cur.execute("SELECT count(*) c FROM payments WHERE bot_id = %s AND status = 'pending'", (bot_id,))
        pending_c = cur.fetchone()["c"]
    tg_send_message(token, chat_id, f"👥 Users: {users_c}\n✅ Active plans: {active_c}\n⏳ Pending payments: {pending_c}")


def handle_command(conn, token, bot_id, chat, text):
    body = text[1:]
    if " " in body:
        cmd, arg = body.split(" ", 1)
    else:
        cmd, arg = body, ""
    cmd = cmd.split("@")[0].lower()
    chat_id = chat["id"]

    if cmd == "help":
        send_help(conn, token, bot_id, chat_id)
        return
    if cmd == "claimadmin":
        claim_admin(conn, token, bot_id, chat)
        return

    if not is_admin_chat(conn, bot_id, chat_id):
        tg_send_message(token, chat_id, "Unknown command. Try /help.")
        return

    if cmd == "admin":
        send_admin_menu(token, chat_id)
    elif cmd == "setprompt":
        cmd_setprompt(conn, token, bot_id, chat_id, arg)
    elif cmd == "setcard":
        cmd_setcard(conn, token, bot_id, chat_id, arg)
    elif cmd == "setcrypto":
        cmd_setcrypto(conn, token, bot_id, chat_id, arg)
    elif cmd == "addplan":
        cmd_addplan(conn, token, bot_id, chat_id, arg)
    elif cmd == "plans":
        cmd_listplans_admin(conn, token, bot_id, chat_id)
    elif cmd == "delplan":
        cmd_delplan(conn, token, bot_id, chat_id, arg)
    elif cmd == "pending":
        cmd_pending(conn, token, bot_id, chat_id)
    elif cmd == "stats":
        cmd_stats(conn, token, bot_id, chat_id)
    else:
        tg_send_message(token, chat_id, "Unknown admin command. Try /admin.")


# ---------------------------------------------------------------------------
# Photo / text (the actual screenshot Q&A)
# ---------------------------------------------------------------------------

def handle_photo(conn, token, bot_id, chat, user_row, message):
    file_id = message["photo"][-1]["file_id"]
    caption = (message.get("caption") or "").strip()

    if user_row["pending_payment_plan_id"]:
        create_payment_and_notify(conn, token, bot_id, chat, user_row, file_id)
        return

    allowed, source = check_entitlement(conn, bot_id, user_row)
    if not allowed:
        tg_send_message(token, chat["id"], "You've used up your free analyses. Choose a plan to continue:")
        send_plans(conn, token, bot_id, chat["id"])
        return

    tg_send_message(token, chat["id"], "🔎 Analyzing your screenshot…")

    try:
        img_bytes = tg_get_file_bytes(token, file_id)
    except requests.RequestException as e:
        print("Failed to download photo:", e)
        tg_send_message(token, chat["id"], "⚠️ Couldn't download that photo from Telegram. Please try sending it again.")
        return

    system_prompt = get_system_prompt(conn, bot_id)
    question = caption or DEFAULT_QUESTION
    answer = gemini_ask(system_prompt, img_bytes, "image/jpeg", question, history=[])

    set_active_image(conn, bot_id, chat["id"], file_id)
    log_message(conn, bot_id, chat["id"], "user", f"[screenshot] {question}")
    log_message(conn, bot_id, chat["id"], "model", answer)
    if source == "free":
        increment_free_used(conn, bot_id, chat["id"])

    tg_send_message(token, chat["id"], answer)


def handle_text(conn, token, bot_id, chat, user_row, text):
    if text == BTN_NEW:
        clear_active_image(conn, bot_id, chat["id"])
        tg_send_message(token, chat["id"], "Send me a new screenshot of a chart or trading position.")
        return
    if text == BTN_PLANS:
        send_plans(conn, token, bot_id, chat["id"])
        return
    if text == BTN_ACCOUNT:
        send_account(conn, token, bot_id, chat, user_row)
        return
    if text == BTN_HELP:
        send_help(conn, token, bot_id, chat["id"])
        return

    if not user_row["active_image_file_id"]:
        tg_send_message(
            token, chat["id"],
            "Send me a screenshot of a chart or trading position first, or use a button below.",
            reply_markup=main_menu_kb(),
        )
        return

    try:
        img_bytes = tg_get_file_bytes(token, user_row["active_image_file_id"])
    except requests.RequestException as e:
        print("Failed to re-download active image:", e)
        tg_send_message(token, chat["id"], "⚠️ Couldn't reload your screenshot. Please send it again.")
        return

    system_prompt = get_system_prompt(conn, bot_id)
    history = get_recent_history(conn, bot_id, chat["id"], user_row["active_image_set_at"])
    answer = gemini_ask(system_prompt, img_bytes, "image/jpeg", text, history)

    log_message(conn, bot_id, chat["id"], "user", text)
    log_message(conn, bot_id, chat["id"], "model", answer)

    tg_send_message(token, chat["id"], answer)


def handle_callback(conn, token, bot_id, cq):
    data = cq.get("data", "")
    message = cq.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    from_chat = cq.get("from") or {}

    get_or_create_user(conn, bot_id, from_chat if "id" in from_chat else {"id": chat_id})

    if data.startswith("plan:"):
        plan_id = int(data.split(":", 1)[1])
        show_payment_method_choice(conn, token, bot_id, chat_id, plan_id)
        tg_answer_callback(token, cq["id"])
        return

    if data.startswith("pay:"):
        _, method, plan_id = data.split(":")
        set_pending_payment(conn, bot_id, chat_id, int(plan_id), method)
        show_payment_instructions(conn, token, bot_id, chat_id, int(plan_id), method)
        tg_answer_callback(token, cq["id"])
        return

    if data.startswith("payapprove:") or data.startswith("payreject:"):
        if not is_admin_chat(conn, bot_id, chat_id):
            tg_answer_callback(token, cq["id"], "Not authorized", show_alert=True)
            return
        payment_id = int(data.split(":", 1)[1])
        approve = data.startswith("payapprove:")
        handle_payment_decision(conn, token, bot_id, payment_id, approve, chat_id, message_id)
        tg_answer_callback(token, cq["id"], "Done")
        return

    tg_answer_callback(token, cq["id"])


def process_update(conn, token, bot_id, update):
    if "callback_query" in update:
        handle_callback(conn, token, bot_id, update["callback_query"])
        return

    message = update.get("message") or update.get("edited_message")
    if not message or "chat" not in message:
        return

    chat = message["chat"]
    user_row, is_new = get_or_create_user(conn, bot_id, chat)
    text = message.get("text")
    photo = message.get("photo")

    if text and text.startswith("/start"):
        parts = text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else None
        handle_start(conn, token, bot_id, chat, is_new, payload)
        return

    if text and text.startswith("/"):
        handle_command(conn, token, bot_id, chat, text)
        return

    if photo:
        handle_photo(conn, token, bot_id, chat, user_row, message)
        return

    if text:
        handle_text(conn, token, bot_id, chat, user_row, text)
        return


# ---------------------------------------------------------------------------
# Web page (same look as the original — paste a token, connect the bot)
# ---------------------------------------------------------------------------

PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trading Analysis Bot</title>
<style>
  :root {
    --tg-blue: #229ED9;
    --tg-blue-dark: #1b87ba;
    --bg: #f4f7f9;
    --card: #ffffff;
    --text: #1c2733;
    --muted: #6b7a89;
    --border: #e2e8ee;
    --green: #1f9d55;
    --red: #d1453b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: var(--text);
    padding: 24px;
  }
  .card {
    width: 100%;
    max-width: 460px;
    background: var(--card);
    border-radius: 16px;
    box-shadow: 0 10px 30px rgba(20, 40, 60, 0.08);
    padding: 32px 28px;
  }
  .logo {
    width: 48px;
    height: 48px;
    border-radius: 12px;
    background: var(--tg-blue);
    display: flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 16px;
  }
  .logo svg { width: 26px; height: 26px; fill: #fff; }
  h1 { font-size: 20px; margin: 0 0 6px; }
  p.sub { color: var(--muted); font-size: 14px; margin: 0 0 24px; line-height: 1.5; }
  label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
  input[type=text] {
    width: 100%;
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: 10px;
    font-size: 14px;
    margin-bottom: 6px;
    outline: none;
    transition: border-color .15s;
  }
  input[type=text]:focus { border-color: var(--tg-blue); }
  .hint { color: var(--muted); font-size: 12px; margin-bottom: 20px; }
  .hint a { color: var(--tg-blue); text-decoration: none; }
  button {
    width: 100%;
    padding: 12px 14px;
    border: none;
    border-radius: 10px;
    background: var(--tg-blue);
    color: #fff;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    transition: background .15s;
  }
  button:hover { background: var(--tg-blue-dark); }
  .banner {
    border-radius: 10px;
    padding: 12px 14px;
    font-size: 13.5px;
    line-height: 1.5;
    margin-bottom: 20px;
  }
  .banner.ok { background: #e9f9ef; color: var(--green); border: 1px solid #bfe9cf; }
  .banner.err { background: #fdecea; color: var(--red); border: 1px solid #f6c6c2; }
  .bot-row {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 14px;
    padding-top: 14px;
    border-top: 1px solid var(--border);
  }
  .bot-row a {
    color: var(--tg-blue);
    font-weight: 600;
    text-decoration: none;
    font-size: 14px;
  }
  form.inline { margin-top: 10px; }
  .disconnect {
    background: transparent;
    color: var(--red);
    border: 1px solid #f2d3d0;
    font-size: 13px;
    padding: 8px 12px;
    width: auto;
  }
  .disconnect:hover { background: #fdecea; }
</style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <svg viewBox="0 0 24 24"><path d="M21.5 3.5 2.7 10.9c-1.2.5-1.2 1.2-.2 1.5l4.8 1.5 1.9 5.7c.2.6.4.8.9.8.5 0 .7-.2 1-.5l2.4-2.3 4.9 3.6c.9.5 1.6.2 1.8-.8L23.9 4.9c.3-1.2-.5-1.8-1.4-1.4-.3.1-.3.1 0 0Zm-3.9 3.8L9 13.9l-.4 3.6-1.6-4.9 11.6-6.1c.5-.3.9-.1.6.5Z"/></svg>
    </div>
    <h1>AI Trading Analysis Bot</h1>
    <p class="sub">Paste your bot's token below. Once connected, people can send it chart/position
      screenshots and ask questions — answered by Gemini — with free trials, referral bonuses,
      and manual card/crypto payment plans.</p>

    {% if message %}
      <div class="banner {{ 'ok' if ok else 'err' }}">{{ message }}</div>
    {% endif %}

    <form method="post" action="/">
      <label for="bot_token">Bot token</label>
      <input type="text" id="bot_token" name="bot_token" placeholder="123456789:AAExampleTokenFromBotFather" value="{{ token_value }}" autocomplete="off" spellcheck="false" required>
      <div class="hint">Don't have one? Message <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> on Telegram and send <code>/newbot</code>.</div>
      <button type="submit">Connect bot</button>
    </form>

    {% if bot_username %}
      <div class="bot-row">
        <a href="https://t.me/{{ bot_username }}" target="_blank" rel="noopener">Open @{{ bot_username }} &rarr;</a>
      </div>
      <form class="inline" method="post" action="/disconnect">
        <input type="hidden" name="bot_token" value="{{ token_value }}">
        <button type="submit" class="disconnect">Disconnect this bot</button>
      </form>
    {% endif %}
  </div>
</body>
</html>
"""


def build_webhook_url(token):
    return f"https://{request.host}/api/webhook/{token}"


@app.route("/", methods=["GET", "POST"])
def index():
    context = {"message": None, "ok": False, "bot_username": None, "token_value": ""}

    if request.method == "POST":
        token = (request.form.get("bot_token") or "").strip()
        context["token_value"] = token

        if not token or not TOKEN_RE.match(token):
            context["message"] = "That doesn't look like a valid bot token. Copy it exactly as BotFather gave it to you."
            return render_template_string(PAGE, **context)

        if not get_db_url():
            context["message"] = (
                "No database is configured yet. Add a DATABASE_URL environment variable in your "
                "Vercel project settings (a Postgres connection string), redeploy, then come back "
                "and connect."
            )
            return render_template_string(PAGE, **context)

        try:
            me = tg_call(token, "getMe")
        except requests.RequestException:
            context["message"] = "Couldn't reach Telegram right now. Please try again."
            return render_template_string(PAGE, **context)

        if not me.get("ok"):
            context["message"] = f"Telegram rejected that token: {me.get('description', 'unknown error')}."
            return render_template_string(PAGE, **context)

        bot_username = me["result"].get("username")
        bot_id = me["result"]["id"]

        try:
            conn = get_conn()
            ensure_schema(conn)
            ensure_bot_row(conn, bot_id, bot_username)
            conn.close()
        except Exception as e:
            context["message"] = f"Connected to Telegram, but the database setup failed: {e}"
            return render_template_string(PAGE, **context)

        try:
            hook = tg_call(token, "setWebhook", url=build_webhook_url(token), drop_pending_updates=True)
        except requests.RequestException:
            context["message"] = "Connected to the bot, but couldn't register the webhook. Please try again."
            return render_template_string(PAGE, **context)

        if not hook.get("ok"):
            context["message"] = f"Couldn't set the webhook: {hook.get('description', 'unknown error')}."
            return render_template_string(PAGE, **context)

        context["ok"] = True
        context["bot_username"] = bot_username
        context["message"] = (
            f"Connected! @{bot_username} is live. Open the bot and send /claimadmin to become its "
            f"admin, then /admin to set up plans, payment details and the AI prompt."
        )

    return render_template_string(PAGE, **context)


@app.route("/disconnect", methods=["POST"])
def disconnect():
    token = (request.form.get("bot_token") or "").strip()
    context = {"message": None, "ok": False, "bot_username": None, "token_value": ""}

    if token and TOKEN_RE.match(token):
        try:
            result = tg_call(token, "deleteWebhook")
            if result.get("ok"):
                context["message"] = "Bot disconnected. It will no longer receive or reply to messages."
                context["ok"] = True
            else:
                context["message"] = f"Couldn't disconnect: {result.get('description', 'unknown error')}."
        except requests.RequestException:
            context["message"] = "Couldn't reach Telegram right now. Please try again."
    else:
        context["message"] = "Missing or invalid token."

    return render_template_string(PAGE, **context)


@app.route("/api/webhook/<token>", methods=["GET", "POST"])
def webhook(token):
    if request.method == "GET":
        return {"ok": True, "info": "This endpoint only accepts Telegram webhook POST requests."}, 200

    if not TOKEN_RE.match(token):
        return {"ok": True}, 200

    bot_id = int(token.split(":", 1)[0])
    update = request.get_json(silent=True) or {}

    conn = None
    try:
        conn = get_conn()
        ensure_schema(conn)
        ensure_bot_row(conn, bot_id)
        process_update(conn, token, bot_id, update)
    except Exception as e:
        print("Error handling update:", e)
    finally:
        if conn:
            conn.close()

    # Always 200 so Telegram doesn't keep retrying this update.
    return {"ok": True}, 200


if __name__ == "__main__":
    app.run(debug=True)
