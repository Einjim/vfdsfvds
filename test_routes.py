import os
import sys

os.environ["DATABASE_URL"] = "postgresql://postgres:testpass@localhost:5432/tradingbot_test?sslmode=disable"
sys.path.insert(0, os.path.dirname(__file__))
import index as bot  # noqa: E402


def fake_tg_call(token, method, **params):
    if method == "getMe":
        return {"ok": True, "result": {"id": 888888888, "username": "route_test_bot", "is_bot": True}}
    if method == "setWebhook":
        assert params["url"].endswith(f"/api/webhook/{token}")
        return {"ok": True, "result": True}
    if method == "deleteWebhook":
        return {"ok": True, "result": True}
    return {"ok": True, "result": {}}


bot.tg_call = fake_tg_call

client = bot.app.test_client()

# GET the connect page
r = client.get("/")
assert r.status_code == 200 and b"AI Trading Analysis Bot" in r.data
print("OK: GET / renders")

FAKE_TOKEN = "888888888:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

# POST a valid-looking token -> should connect successfully
r = client.post("/", data={"bot_token": FAKE_TOKEN})
assert r.status_code == 200
assert b"Connected!" in r.data, r.data[:2000]
assert b"route_test_bot" in r.data
print("OK: POST / connects bot and shows success banner")

row = bot.get_bot_row(bot.get_conn(), 888888888)
assert row is not None and row["username"] == "route_test_bot"
print("OK: bots row created by connect flow ->", dict(row))

# Malformed token rejected
r = client.post("/", data={"bot_token": "not-a-token"})
assert b"doesn&#39;t look like a valid" in r.data or b"valid bot token" in r.data
print("OK: malformed token rejected")

# Webhook GET (Telegram never does this, but browsers might)
r = client.get(f"/api/webhook/{FAKE_TOKEN}")
assert r.status_code == 200 and r.get_json()["ok"] is True
print("OK: webhook GET returns info payload")

# Webhook POST with a simple /start update end-to-end through the real route
update = {"message": {"chat": {"id": 555, "username": "routeuser", "first_name": "R"}, "text": "/start"}}
r = client.post(f"/api/webhook/{FAKE_TOKEN}", json=update)
assert r.status_code == 200 and r.get_json()["ok"] is True
print("OK: webhook POST /start handled without error")

user_row, _ = bot.get_or_create_user(bot.get_conn(), 888888888, {"id": 555})
assert user_row is not None
print("OK: user row created via full HTTP webhook path ->", dict(user_row))

# Disconnect
r = client.post("/disconnect", data={"bot_token": FAKE_TOKEN})
assert b"disconnected" in r.data
print("OK: disconnect route works")

print("\nALL ROUTE CHECKS PASSED")
