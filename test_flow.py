import os
import sys

os.environ["DATABASE_URL"] = "postgresql://postgres:testpass@localhost:5432/tradingbot_test?sslmode=disable"

sys.path.insert(0, os.path.dirname(__file__))
import index as bot  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes for external network calls (Telegram + Gemini)
# ---------------------------------------------------------------------------

SENT_MESSAGES = []  # list of dicts: chat_id, text/caption, reply_markup
TG_CALL_LOG = []


def fake_tg_call(token, method, **params):
    TG_CALL_LOG.append((method, params))
    if method == "sendMessage":
        SENT_MESSAGES.append({"kind": "text", "chat_id": params["chat_id"], "text": params["text"]})
        return {"ok": True, "result": {"message_id": len(TG_CALL_LOG)}}
    if method == "sendPhoto":
        SENT_MESSAGES.append({"kind": "photo", "chat_id": params["chat_id"], "caption": params.get("caption")})
        return {"ok": True, "result": {"message_id": len(TG_CALL_LOG)}}
    if method == "editMessageCaption":
        SENT_MESSAGES.append({"kind": "edit_caption", "chat_id": params["chat_id"], "caption": params["caption"]})
        return {"ok": True, "result": {}}
    if method in ("answerCallbackQuery",):
        return {"ok": True, "result": True}
    if method == "getFile":
        return {"ok": True, "result": {"file_path": "fake/path.jpg"}}
    return {"ok": True, "result": {}}


def fake_get_file_bytes(token, file_id):
    return b"FAKE_IMAGE_BYTES"


def fake_gemini_ask(system_prompt, image_bytes, mime_type, question, history):
    return f"[FAKE ANSWER to: {question!r} | history_len={len(history)}]"


bot.tg_call = fake_tg_call
bot.tg_get_file_bytes = fake_get_file_bytes
bot.gemini_ask = fake_gemini_ask

BOT_ID = 999999999
TOKEN = f"{BOT_ID}:FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE"

ADMIN_CHAT = {"id": 1001, "username": "admin_user", "first_name": "Admin"}
ALICE = {"id": 2002, "username": "alice", "first_name": "Alice"}
REFS = [{"id": 3000 + i, "username": f"ref{i}", "first_name": f"Ref{i}"} for i in range(5)]


def msg(chat, text=None, photo_file_id=None, caption=None):
    m = {"chat": chat, "from": chat}
    if text is not None:
        m["text"] = text
    if photo_file_id is not None:
        m["photo"] = [{"file_id": photo_file_id, "width": 100, "height": 100}]
        if caption is not None:
            m["caption"] = caption
    return {"message": m}


def cq(chat, data, message_id=1, from_user=None):
    return {
        "callback_query": {
            "id": "cqid",
            "data": data,
            "from": from_user or chat,
            "message": {"chat": chat, "message_id": message_id},
        }
    }


def run():
    conn = bot.get_conn()
    bot.ensure_schema(conn)
    bot.ensure_bot_row(conn, BOT_ID, "test_trading_bot")

    # 1. Admin claims admin
    bot.process_update(conn, TOKEN, BOT_ID, msg(ADMIN_CHAT, text="/claimadmin"))
    row = bot.get_bot_row(conn, BOT_ID)
    assert row["admin_chat_id"] == ADMIN_CHAT["id"], "admin claim failed"
    print("OK: admin claimed ->", row["admin_chat_id"])

    # 2. Admin sets prompt / payment info / adds a plan
    bot.process_update(conn, TOKEN, BOT_ID, msg(ADMIN_CHAT, text="/setprompt You are a test analyst."))
    bot.process_update(conn, TOKEN, BOT_ID, msg(ADMIN_CHAT, text="/setcard Card 1234-5678, name: Test"))
    bot.process_update(conn, TOKEN, BOT_ID, msg(ADMIN_CHAT, text="/setcrypto USDT TRC20: Txxxxxx"))
    bot.process_update(conn, TOKEN, BOT_ID, msg(ADMIN_CHAT, text="/addplan Monthly | 500000 Toman | 30"))
    plans = bot.list_active_plans(conn, BOT_ID)
    assert len(plans) == 1 and plans[0]["name"] == "Monthly", "plan not added"
    plan_id = plans[0]["id"]
    print("OK: plan added ->", dict(plans[0]))

    # 3. Alice starts the bot with a referral payload pointing at herself (should be ignored)
    bot.process_update(conn, TOKEN, BOT_ID, msg(ALICE, text=f"/start {ALICE['id']}"))
    alice_row, _ = bot.get_or_create_user(conn, BOT_ID, ALICE)
    assert bot.get_referral_count(conn, BOT_ID, ALICE["id"]) == 0
    print("OK: self-referral ignored")

    # 4. Five referred users start via Alice's link
    for r in REFS:
        bot.process_update(conn, TOKEN, BOT_ID, msg(r, text=f"/start {ALICE['id']}"))
    ref_count = bot.get_referral_count(conn, BOT_ID, ALICE["id"])
    assert ref_count == 5, f"expected 5 referrals, got {ref_count}"
    print("OK: referral count ->", ref_count)

    # 4b. Re-sending /start with same payload for an existing referred user shouldn't double count
    bot.process_update(conn, TOKEN, BOT_ID, msg(REFS[0], text=f"/start {ALICE['id']}"))
    assert bot.get_referral_count(conn, BOT_ID, ALICE["id"]) == 5
    print("OK: duplicate referral not double-counted")

    # 5. Entitlement check: Alice should now have 1 base + 1 bonus (5 referrals) = 2 free credits
    allowed, source = bot.check_entitlement(conn, BOT_ID, bot.get_or_create_user(conn, BOT_ID, ALICE)[0])
    assert allowed and source == "free"
    print("OK: entitlement ->", source)

    # 6. Alice sends a screenshot (consumes 1 free credit) and asks a follow-up
    bot.process_update(conn, TOKEN, BOT_ID, msg(ALICE, photo_file_id="PHOTO1", caption="what do you see?"))
    alice_row, _ = bot.get_or_create_user(conn, BOT_ID, ALICE)
    assert alice_row["free_used_count"] == 1, alice_row["free_used_count"]
    assert alice_row["active_image_file_id"] == "PHOTO1"
    print("OK: first screenshot analyzed, free_used_count ->", alice_row["free_used_count"])

    bot.process_update(conn, TOKEN, BOT_ID, msg(ALICE, text="what's the RSI doing?"))
    hist = bot.get_recent_history(conn, BOT_ID, ALICE["id"], alice_row["active_image_set_at"])
    assert len(hist) >= 2, "history should include prior Q&A"
    print("OK: follow-up question answered without consuming a new credit; history len ->", len(hist))
    alice_row, _ = bot.get_or_create_user(conn, BOT_ID, ALICE)
    assert alice_row["free_used_count"] == 1, "follow-up should NOT consume another free credit"

    # 7. Second screenshot consumes bonus credit (2nd of 2 free credits)
    bot.process_update(conn, TOKEN, BOT_ID, msg(ALICE, photo_file_id="PHOTO2"))
    alice_row, _ = bot.get_or_create_user(conn, BOT_ID, ALICE)
    assert alice_row["free_used_count"] == 2, alice_row["free_used_count"]
    print("OK: second screenshot used bonus credit, free_used_count ->", alice_row["free_used_count"])

    # 8. Third screenshot should be blocked (no credits left, no plan)
    allowed, source = bot.check_entitlement(conn, BOT_ID, alice_row)
    assert not allowed
    print("OK: entitlement exhausted as expected")

    # 9. Alice buys the Monthly plan: picks plan -> picks card -> sends proof photo
    bot.process_update(conn, TOKEN, BOT_ID, cq(ALICE, f"plan:{plan_id}"))
    bot.process_update(conn, TOKEN, BOT_ID, cq(ALICE, f"pay:card:{plan_id}"))
    alice_row, _ = bot.get_or_create_user(conn, BOT_ID, ALICE)
    assert alice_row["pending_payment_plan_id"] == plan_id
    bot.process_update(conn, TOKEN, BOT_ID, msg(ALICE, photo_file_id="PROOF1"))
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM payments WHERE bot_id=%s AND user_telegram_id=%s", (BOT_ID, ALICE["id"]))
        payment = cur.fetchone()
    assert payment and payment["status"] == "pending", payment
    payment_id = payment["id"]
    print("OK: payment submitted ->", dict(payment))

    alice_row, _ = bot.get_or_create_user(conn, BOT_ID, ALICE)
    assert alice_row["pending_payment_plan_id"] is None, "pending flag should clear after proof submitted"

    # 10. Non-admin tries to approve -> must be rejected
    bot.process_update(conn, TOKEN, BOT_ID, cq(ALICE, f"payapprove:{payment_id}"))
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM payments WHERE id=%s", (payment_id,))
        assert cur.fetchone()["status"] == "pending", "non-admin should not be able to approve"
    print("OK: non-admin approve blocked")

    # 11. Admin approves -> plan activated
    bot.process_update(conn, TOKEN, BOT_ID, cq(ADMIN_CHAT, f"payapprove:{payment_id}", message_id=42))
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM payments WHERE id=%s", (payment_id,))
        payment = cur.fetchone()
    assert payment["status"] == "approved"
    alice_row, _ = bot.get_or_create_user(conn, BOT_ID, ALICE)
    assert alice_row["active_plan_id"] == plan_id
    assert alice_row["plan_expires_at"] is not None
    print("OK: payment approved, plan active until", alice_row["plan_expires_at"])

    # 12. Now Alice should be entitled via 'plan', regardless of used-up free credits
    allowed, source = bot.check_entitlement(conn, BOT_ID, alice_row)
    assert allowed and source == "plan"
    print("OK: entitlement now via active plan")

    # 13. Rejection path with a second plan purchase attempt (new user Bob)
    bob = {"id": 4004, "username": "bob", "first_name": "Bob"}
    bot.process_update(conn, TOKEN, BOT_ID, msg(bob, text="/start"))
    bot.process_update(conn, TOKEN, BOT_ID, cq(bob, f"plan:{plan_id}"))
    bot.process_update(conn, TOKEN, BOT_ID, cq(bob, f"pay:crypto:{plan_id}"))
    bot.process_update(conn, TOKEN, BOT_ID, msg(bob, photo_file_id="PROOF_BOB"))
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM payments WHERE bot_id=%s AND user_telegram_id=%s", (BOT_ID, bob["id"]))
        bob_payment = cur.fetchone()
    bot.process_update(conn, TOKEN, BOT_ID, cq(ADMIN_CHAT, f"payreject:{bob_payment['id']}", message_id=43))
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM payments WHERE id=%s", (bob_payment["id"],))
        assert cur.fetchone()["status"] == "rejected"
    bob_row, _ = bot.get_or_create_user(conn, BOT_ID, bob)
    assert bob_row["active_plan_id"] is None
    print("OK: rejection path works, bob has no active plan")

    # 14. Admin utility commands don't crash
    bot.process_update(conn, TOKEN, BOT_ID, msg(ADMIN_CHAT, text="/stats"))
    bot.process_update(conn, TOKEN, BOT_ID, msg(ADMIN_CHAT, text="/plans"))
    bot.process_update(conn, TOKEN, BOT_ID, msg(ADMIN_CHAT, text="/pending"))
    bot.process_update(conn, TOKEN, BOT_ID, msg(ADMIN_CHAT, text=f"/delplan {plan_id}"))
    assert bot.list_active_plans(conn, BOT_ID) == []
    print("OK: admin utility commands ran without error, plan deactivated")

    # 15. Non-admin trying an admin command is refused
    before = len(SENT_MESSAGES)
    bot.process_update(conn, TOKEN, BOT_ID, msg(bob, text="/setprompt hacked"))
    row = bot.get_bot_row(conn, BOT_ID)
    assert row["system_prompt"] == "You are a test analyst."
    print("OK: non-admin cannot change system prompt")

    conn.close()
    print("\nALL CHECKS PASSED (%d telegram API calls simulated, %d messages sent)" % (len(TG_CALL_LOG), len(SENT_MESSAGES)))


if __name__ == "__main__":
    run()
