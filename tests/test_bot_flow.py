"""End-to-end: real handlers, fake Telegram (BaseRequest) and fake game4station."""
from __future__ import annotations

import json
from decimal import Decimal
from itertools import count

import pytest
from telegram import Update
from telegram.request import BaseRequest

from bot.config import Settings
from bot.g4s import G4SClient
from bot.main import build_app
from bot.security import generate_key
from bot.store import MemoryStore
from tests.fakes import FakeG4S

OWNER = 111111111
CUSTOMER = 222222222
STRANGER = 333333333


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._ids = count(1000)

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    @property
    def read_timeout(self):
        return 5.0

    async def do_request(self, url, method, request_data=None, read_timeout=None,
                         write_timeout=None, connect_timeout=None, pool_timeout=None):
        name = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((name, params))
        if name == "getMe":
            result = {"id": 999, "is_bot": True, "first_name": "Bot", "username": "test_bot"}
        elif name in {"sendMessage", "editMessageText", "editMessageReplyMarkup"}:
            result = {"message_id": next(self._ids), "date": 0, "text": params.get("text", ""),
                      "chat": {"id": params.get("chat_id", 0), "type": "private"}}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def sent(self, chat_id=None):
        return [p for n, p in self.calls
                if n in {"sendMessage", "editMessageText"}
                and (chat_id is None or p.get("chat_id") == chat_id)]

    def last_text(self, chat_id):
        return self.sent(chat_id)[-1]["text"]

    def last_buttons(self, chat_id):
        for p in reversed(self.sent(chat_id)):
            markup = p.get("reply_markup")
            if markup and "inline_keyboard" in markup:
                return [b for row in markup["inline_keyboard"] for b in row]
        return []

    def alerts(self):
        return [p for n, p in self.calls if n == "answerCallbackQuery" and p.get("show_alert")]


class Harness:
    def __init__(self):
        self.tg = FakeTelegram()
        self.g4s = FakeG4S()
        self.store = MemoryStore()
        self.settings = Settings(
            bot_token="123:TEST", g4s_api_token="test-token", owner_ids=frozenset({OWNER}),
            encryption_keys=f"v1:{generate_key()}", hmac_key=generate_key(),
            support_contact="@support")
        self.app = build_app(self.settings, store=self.store,
                             api=G4SClient("test-token", transport=self.g4s.transport()),
                             request=self.tg)
        self._uid = count(1)

    @property
    def svc(self):
        return self.app.bot_data["svc"]

    def _user(self, uid):
        return {"id": uid, "is_bot": False, "first_name": f"U{uid}", "username": f"u{uid}"}

    async def text(self, uid, text):
        msg = {"message_id": next(self._uid), "date": 0, "text": text,
               "chat": {"id": uid, "type": "private"}, "from": self._user(uid)}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.app.process_update(Update.de_json(
            {"update_id": next(self._uid), "message": msg}, self.app.bot))

    async def press(self, uid, data):
        cq = {"id": str(next(self._uid)), "from": self._user(uid), "chat_instance": "c", "data": data,
              "message": {"message_id": 1, "date": 0, "text": "x",
                          "chat": {"id": uid, "type": "private"},
                          "from": {"id": 999, "is_bot": True, "first_name": "Bot"}}}
        await self.app.process_update(Update.de_json(
            {"update_id": next(self._uid), "callback_query": cq}, self.app.bot))


@pytest.fixture
async def h():
    harness = Harness()
    await harness.app.initialize()
    yield harness
    await harness.app.shutdown()


async def test_full_purchase_flow(h):
    await h.text(CUSTOMER, "/start")
    assert "أهلاً" in h.tg.last_text(CUSTOMER)

    await h.text(OWNER, "/start")
    await h.text(OWNER, f"/credit {CUSTOMER} 10 تحويل بنكي")
    assert "10.00" in h.tg.last_text(OWNER)
    assert "10.00" in h.tg.last_text(CUSTOMER)  # customer notified

    await h.text(CUSTOMER, "🛒 المنتجات")
    cats = h.tg.last_buttons(CUSTOMER)
    pubg = next(b for b in cats if b["text"] == "PUBG Global ID UC")
    await h.press(CUSTOMER, pubg["callback_data"])
    product_btn = next(b for b in h.tg.last_buttons(CUSTOMER) if b["callback_data"] == "p:18")
    assert "1.21" in product_btn["text"]

    await h.press(CUSTOMER, "p:18")
    assert "ادخل الايدي الاعب" in h.tg.last_text(CUSTOMER)
    await h.text(CUSTOMER, "5123456789")
    assert "تأكيد الطلب" in h.tg.last_text(CUSTOMER)
    confirm = next(b for b in h.tg.last_buttons(CUSTOMER) if b["callback_data"].startswith("o:"))

    await h.press(CUSTOMER, confirm["callback_data"])
    assert "تم تنفيذ طلبك بنجاح" in h.tg.last_text(CUSTOMER)
    assert "CODE-123" in h.tg.last_text(CUSTOMER)
    newest = h.g4s.requests[-1]
    assert newest.url.params["playerId"] == "5123456789"

    # Double tap on the same confirm button: no second charge.
    await h.press(CUSTOMER, confirm["callback_data"])
    assert len(h.g4s.orders) == 1
    assert (await h.svc.repo.get_user(CUSTOMER)).balance == Decimal("8.79")

    await h.text(CUSTOMER, "💰 رصيدي")
    assert "8.79" in h.tg.last_text(CUSTOMER)
    await h.text(CUSTOMER, "🧾 سجل المحفظة")
    assert "طلب" in h.tg.last_text(CUSTOMER) and "شحن" in h.tg.last_text(CUSTOMER)
    await h.text(CUSTOMER, "📦 طلباتي")
    assert h.tg.last_buttons(CUSTOMER)[0]["callback_data"].startswith("od:")


async def test_range_quantity_flow(h):
    await h.text(CUSTOMER, "/start")
    await h.text(OWNER, f"/credit {CUSTOMER} 200")
    await h.press(CUSTOMER, "p:365")
    await h.text(CUSTOMER, "PLAYER1")
    assert "اكتب الكمية" in h.tg.last_text(CUSTOMER)
    await h.text(CUSTOMER, "999999")
    assert "أعلى كمية" in h.tg.last_text(CUSTOMER)
    await h.text(CUSTOMER, "١٠٠٠")  # Arabic digits
    assert "114.40" in h.tg.last_text(CUSTOMER)


async def test_owner_commands_are_ignored_for_customers(h):
    await h.text(CUSTOMER, "/start")
    before = len(h.tg.sent(CUSTOMER))
    await h.text(CUSTOMER, f"/credit {CUSTOMER} 1000")
    await h.text(CUSTOMER, "🛠 لوحة المالك")  # typing the hidden button text
    await h.press(CUSTOMER, "a:mt")
    assert (await h.svc.repo.get_user(CUSTOMER)).balance == Decimal("0")
    assert not await h.svc.runtime.maintenance()
    # the typed button falls through to the generic menu reply only
    assert all("لوحة المالك" not in p["text"] for p in h.tg.sent(CUSTOMER)[before:])


async def test_maintenance_blocks_customers_not_owner(h):
    await h.text(OWNER, "/maintenance on")
    await h.text(CUSTOMER, "🛒 المنتجات")
    assert "الصيانة" in h.tg.last_text(CUSTOMER)
    await h.text(OWNER, "/maintenance off")
    await h.text(CUSTOMER, "🛒 المنتجات")
    assert "اختر القسم" in h.tg.last_text(CUSTOMER)
    logs = await h.svc.repo.owner_logs()
    assert [a for _, _, a, _ in logs] == ["maintenance", "maintenance"]


async def test_ban_and_rate_limit(h):
    await h.text(CUSTOMER, "/start")
    await h.text(OWNER, f"/ban {CUSTOMER}")
    before = len(h.tg.sent(CUSTOMER))
    await h.text(CUSTOMER, "💰 رصيدي")
    assert len(h.tg.sent(CUSTOMER)) == before  # banned users get no reply
    await h.text(OWNER, f"/unban {CUSTOMER}")

    for _ in range(h.settings.actions_per_minute + 5):
        await h.text(STRANGER, "💰 رصيدي")
    assert len(h.tg.sent(STRANGER)) == h.settings.actions_per_minute


async def test_insufficient_balance_message(h):
    await h.text(CUSTOMER, "/start")
    await h.press(CUSTOMER, "p:18")
    await h.text(CUSTOMER, "PLAYER1")
    confirm = next(b for b in h.tg.last_buttons(CUSTOMER) if b["callback_data"].startswith("o:"))
    await h.press(CUSTOMER, confirm["callback_data"])
    assert "رصيدك ما يكفي" in h.tg.last_text(CUSTOMER)
    assert h.g4s.orders == {}
