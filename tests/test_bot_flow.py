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
    await harness.svc.repo.set_products_visible([18, 365], True)
    yield harness
    await harness.app.shutdown()


async def test_full_purchase_flow(h):
    await h.text(CUSTOMER, "/start")
    assert "أهلاً" in h.tg.last_text(CUSTOMER)
    assert f"🆔: <code>{CUSTOMER}</code>" in h.tg.last_text(CUSTOMER)

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
    assert "✍️ ايدي الشحن:" in h.tg.last_text(CUSTOMER)
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


async def test_customers_see_only_visible_products(h):
    await h.svc.repo.set_products_visible([365], False)
    h.svc.runtime.invalidate()
    await h.text(CUSTOMER, "🛒 المنتجات")
    names = [b["text"] for b in h.tg.last_buttons(CUSTOMER)]
    assert names == ["PUBG Global ID UC"]  # category "UC 60" (only product 365) is gone

    await h.press(CUSTOMER, "p:365")  # old or crafted button
    assert "غير متوفر" in h.tg.last_text(CUSTOMER)

    await h.svc.repo.set_products_visible([18], False)
    h.svc.runtime.invalidate()
    await h.text(CUSTOMER, "🛒 المنتجات")
    assert "ما فيه منتجات" in h.tg.last_text(CUSTOMER)


async def test_owner_toggles_product_visibility(h):
    await h.svc.repo.set_products_visible([18, 365], False)
    h.svc.runtime.invalidate()
    await h.text(OWNER, "/products")
    cats = h.tg.last_buttons(OWNER)
    pubg = next(b for b in cats if "PUBG Global ID UC" in b["text"])
    assert pubg["text"].startswith("⬜")

    await h.press(OWNER, pubg["callback_data"])
    toggle = next(b for b in h.tg.last_buttons(OWNER) if b["callback_data"].endswith(":18"))
    assert toggle["text"].startswith("⬜")
    await h.press(OWNER, toggle["callback_data"])
    assert await h.svc.runtime.visible_products() == frozenset({18})
    toggle = next(b for b in h.tg.last_buttons(OWNER) if b["callback_data"].endswith(":18"))
    assert toggle["text"].startswith("✅")

    await h.text(CUSTOMER, "🛒 المنتجات")
    assert [b["text"] for b in h.tg.last_buttons(CUSTOMER)] == ["PUBG Global ID UC"]

    hide_all = next(b for b in h.tg.last_buttons(OWNER) if b["callback_data"].endswith(":0")
                    and b["callback_data"].startswith("a:pa:"))
    await h.press(OWNER, hide_all["callback_data"])
    assert await h.svc.runtime.visible_products() == frozenset()
    logs = [a for _, _, a, _ in await h.svc.repo.owner_logs()]
    assert "product_show" in logs and "category_hide" in logs


async def test_customer_cannot_toggle_visibility(h):
    await h.text(CUSTOMER, "/start")
    key = next(iter(await h.svc.catalog.categories())).key
    await h.press(CUSTOMER, f"a:pa:{key}:0:0")
    await h.press(CUSTOMER, f"a:pt:{key}:0:365")
    h.svc.runtime.invalidate()
    assert await h.svc.runtime.visible_products() == frozenset({18, 365})  # unchanged


async def test_owner_credit_with_buttons(h):
    await h.text(CUSTOMER, "/start")
    await h.press(OWNER, "a:cr")
    assert "آيدي العميل" in h.tg.last_text(OWNER)
    await h.text(OWNER, "abc")
    assert "أرقام" in h.tg.last_text(OWNER)
    await h.text(OWNER, str(STRANGER))  # never pressed /start
    assert "غير مسجّل" in h.tg.last_text(OWNER)
    await h.text(OWNER, str(CUSTOMER))
    assert "أرسل المبلغ" in h.tg.last_text(OWNER)
    await h.text(OWNER, "-5")
    assert "مبلغ غير صالح" in h.tg.last_text(OWNER)
    await h.text(OWNER, "٢٥ تحويل بنكي")  # Arabic digits + note
    assert "تأكيد الشحن" in h.tg.last_text(OWNER)
    assert (await h.svc.repo.get_user(CUSTOMER)).balance == Decimal("0")  # not before confirm

    confirm = next(b for b in h.tg.last_buttons(OWNER) if b["callback_data"].startswith("a:cf:"))
    await h.press(OWNER, confirm["callback_data"])
    assert (await h.svc.repo.get_user(CUSTOMER)).balance == Decimal("25")
    assert "25.00" in h.tg.last_text(CUSTOMER)  # customer notified

    await h.press(OWNER, confirm["callback_data"])  # double tap
    assert (await h.svc.repo.get_user(CUSTOMER)).balance == Decimal("25")
    assert "انتهت صلاحية" in h.tg.last_text(OWNER)
    ledger = await h.svc.repo.ledger_for_user(CUSTOMER)
    assert [(e.type, e.note) for e in ledger] == [("owner_credit", "تحويل بنكي")]


async def test_owner_debit_cancel_and_insufficient(h):
    await h.text(CUSTOMER, "/start")
    await h.press(OWNER, "a:db")
    await h.text(OWNER, str(CUSTOMER))
    await h.press(OWNER, "a:cx")
    assert "تم الإلغاء" in h.tg.last_text(OWNER)
    await h.text(OWNER, "5")  # prompt is gone: plain text no longer means an amount
    assert "اختر من القائمة" in h.tg.last_text(OWNER)

    await h.press(OWNER, "a:db")
    await h.text(OWNER, str(CUSTOMER))
    await h.text(OWNER, "5")
    assert "ما يكفي" in h.tg.last_text(OWNER)  # no confirm button offered
    assert not any(b["callback_data"].startswith("a:cf:") for b in h.tg.last_buttons(OWNER))
    assert (await h.svc.repo.get_user(CUSTOMER)).balance == Decimal("0")


async def test_owner_manage_user_card(h):
    await h.text(CUSTOMER, "/start")
    await h.press(OWNER, "a:us")
    await h.text(OWNER, str(CUSTOMER))
    assert str(CUSTOMER) in h.tg.last_text(OWNER)
    data = [b["callback_data"] for b in h.tg.last_buttons(OWNER)]
    assert f"a:ub:{CUSTOMER}" in data and f"a:uc:{CUSTOMER}" in data

    await h.press(OWNER, f"a:ub:{CUSTOMER}")
    assert (await h.svc.repo.get_user(CUSTOMER)).banned
    assert any(b["text"] == "✅ فك الحظر" for b in h.tg.last_buttons(OWNER))
    await h.press(OWNER, f"a:ub:{CUSTOMER}")
    assert not (await h.svc.repo.get_user(CUSTOMER)).banned

    await h.press(OWNER, f"a:uc:{CUSTOMER}")  # credit straight from the card
    await h.text(OWNER, "3")
    confirm = next(b for b in h.tg.last_buttons(OWNER) if b["callback_data"].startswith("a:cf:"))
    await h.press(OWNER, confirm["callback_data"])
    assert (await h.svc.repo.get_user(CUSTOMER)).balance == Decimal("3")


async def test_owner_margin_buttons(h):
    await h.press(OWNER, "a:mg")
    assert "نسبة الربح الحالية: 10%" in h.tg.last_text(OWNER)
    await h.press(OWNER, "a:ms:15")
    assert await h.svc.runtime.margin() == Decimal("15")
    await h.press(OWNER, "a:ms:999")  # not a preset: ignored
    assert await h.svc.runtime.margin() == Decimal("15")
    await h.press(OWNER, "a:mx")
    await h.text(OWNER, "١٢٫٥")
    assert await h.svc.runtime.margin() == Decimal("12.5")


async def test_owner_order_search_and_pending_buttons(h):
    await h.press(OWNER, "a:od")
    await h.text(OWNER, "not-a-uuid")
    assert "غير صالح" in h.tg.last_text(OWNER)
    await h.text(OWNER, "11111111-1111-4111-8111-111111111111")
    assert "غير موجود" in h.tg.last_text(OWNER)
    await h.press(OWNER, "a:open")
    assert "ما فيه طلبات معلّقة" in h.tg.last_text(OWNER)


async def test_customer_cannot_use_owner_buttons(h):
    await h.text(CUSTOMER, "/start")
    before = len(h.tg.sent(CUSTOMER))
    for data in ("a:cr", "a:us", "a:mg", "a:ms:50", f"a:ub:{CUSTOMER}", "a:cf:deadbeef"):
        await h.press(CUSTOMER, data)
    await h.text(CUSTOMER, str(CUSTOMER))  # would be an ID answer for an owner prompt
    sent = [p["text"] for p in h.tg.sent(CUSTOMER)[before:]]
    assert all("آيدي العميل" not in t and "تأكيد" not in t for t in sent)
    assert await h.svc.runtime.margin() == Decimal("10")
    assert not (await h.svc.repo.get_user(CUSTOMER)).banned


async def test_owner_blocks_player_and_customer_is_refused(h):
    await h.text(CUSTOMER, "/start")
    await h.text(OWNER, f"/credit {CUSTOMER} 10")
    await h.press(OWNER, "a:bl")
    assert "ما فيه آيديات محظورة" in h.tg.last_text(OWNER)
    await h.press(OWNER, "a:ba")
    await h.text(OWNER, "5123456789")
    assert any("تم حظر الآيدي" in p["text"] for p in h.tg.sent(OWNER)[-2:])
    assert "5123456789" in h.tg.last_text(OWNER)  # shown in the list

    await h.press(CUSTOMER, "p:18")
    await h.text(CUSTOMER, "5123456789")
    assert h.tg.last_text(CUSTOMER) == "🚫 هذا الآيدي محظور أو نصاب، وتم إلغاء طلبك."
    assert "آيدي محظور" in h.tg.last_text(OWNER)  # owner alerted
    await h.text(CUSTOMER, "1")  # draft is gone: nothing continues
    assert "اختر من القائمة" in h.tg.last_text(CUSTOMER)
    assert h.g4s.orders == {}
    assert (await h.svc.repo.get_user(CUSTOMER)).balance == Decimal("10.00")

    await h.press(OWNER, "a:bl")
    unblock = next(b for b in h.tg.last_buttons(OWNER) if b["callback_data"].startswith("a:bu:"))
    await h.press(OWNER, unblock["callback_data"])
    assert not await h.svc.repo.is_player_blocked("5123456789")
    await h.press(CUSTOMER, "p:18")
    await h.text(CUSTOMER, "5123456789")
    assert "تأكيد الطلب" in h.tg.last_text(CUSTOMER)
    logs = [a for _, _, a, _ in await h.svc.repo.owner_logs()]
    assert "player_block" in logs and "player_unblock" in logs


async def test_block_between_entering_id_and_confirming(h):
    await h.text(CUSTOMER, "/start")
    await h.text(OWNER, f"/credit {CUSTOMER} 10")
    await h.press(CUSTOMER, "p:18")
    await h.text(CUSTOMER, "777")
    confirm = next(b for b in h.tg.last_buttons(CUSTOMER) if b["callback_data"].startswith("o:"))
    await h.svc.repo.block_player("777", OWNER)
    await h.press(CUSTOMER, confirm["callback_data"])
    assert h.tg.last_text(CUSTOMER) == "🚫 هذا الآيدي محظور أو نصاب، وتم إلغاء طلبك."
    assert h.g4s.orders == {}
    assert (await h.svc.repo.get_user(CUSTOMER)).balance == Decimal("10.00")


async def test_block_player_from_order_card(h):
    await h.text(CUSTOMER, "/start")
    await h.text(OWNER, f"/credit {CUSTOMER} 10")
    await h.press(CUSTOMER, "p:18")
    await h.text(CUSTOMER, "999888")
    confirm = next(b for b in h.tg.last_buttons(CUSTOMER) if b["callback_data"].startswith("o:"))
    await h.press(CUSTOMER, confirm["callback_data"])
    order_uuid = next(iter(h.g4s.orders))

    await h.press(OWNER, f"a:oi:{order_uuid}")
    assert any(b["callback_data"] == f"a:bo:{order_uuid}" for b in h.tg.last_buttons(OWNER))
    await h.press(OWNER, f"a:bo:{order_uuid}")
    assert "تم حظر الآيدي" in h.tg.last_text(OWNER)
    assert await h.svc.repo.is_player_blocked("999888")


async def test_customer_cannot_manage_blocklist(h):
    await h.svc.repo.block_player("555", OWNER)
    [(tag, _, _)] = await h.svc.repo.blocked_players()
    await h.text(CUSTOMER, "/start")
    await h.press(CUSTOMER, "a:ba")
    await h.text(CUSTOMER, "123")
    await h.press(CUSTOMER, f"a:bu:{tag}")
    assert await h.svc.repo.is_player_blocked("555")
    assert not await h.svc.repo.is_player_blocked("123")



async def test_order_messages_show_the_label_the_customer_saw(h):
    await h.text(CUSTOMER, "/start")
    await h.text(OWNER, f"/credit {CUSTOMER} 10")
    await h.press(CUSTOMER, "p:18")
    await h.text(CUSTOMER, "5123456789")
    confirm = next(b for b in h.tg.last_buttons(CUSTOMER) if b["callback_data"].startswith("o:"))
    await h.press(CUSTOMER, confirm["callback_data"])
    text = h.tg.last_text(CUSTOMER)
    assert "تم تنفيذ طلبك بنجاح" in text
    assert "ايدي الشحن: <code>5123456789</code>" in text
    assert "ادخل الايدي الاعب" not in text
    assert "playerId" not in text


async def test_topup_and_confirm_texts(h):
    await h.text(CUSTOMER, "➕ شحن الرصيد")
    text = h.tg.last_text(CUSTOMER)
    assert "طرق الدفع" in text and "USDT" in text
    assert f"ايديك تيليجرام: <code>{CUSTOMER}</code>" in text
    assert "طلب شحن محفظة: @support" in text

    await h.text(OWNER, f"/credit {CUSTOMER} 10")
    await h.press(CUSTOMER, "p:18")
    await h.text(CUSTOMER, "5123456789")
    confirm = h.tg.last_text(CUSTOMER)
    assert "تأكيد الطلب" in confirm and "ايدي الشحن: <code>5123456789</code>" in confirm


async def _pubg_key(h):
    return next(c.key for c in await h.svc.catalog.categories() if c.name == "PUBG Global ID UC")


async def test_owner_renames_category_and_product(h):
    key = await _pubg_key(h)
    await h.text(CUSTOMER, "/start")
    await h.text(OWNER, f"/credit {CUSTOMER} 10")

    await h.press(OWNER, f"a:cn:{key}:0")
    assert "تغيير اسم القسم" in h.tg.last_text(OWNER)
    await h.text(OWNER, "شدات ببجي فوري ID")
    assert "(اسمه عند المزود: PUBG Global ID UC)" in h.tg.last_text(OWNER)

    await h.press(OWNER, f"a:pn:{key}:0:18")
    await h.text(OWNER, "60 شده")
    assert any(b["text"].startswith("✅ 60 شده") for b in h.tg.last_buttons(OWNER))

    await h.text(CUSTOMER, "🛒 المنتجات")
    names = [b["text"] for b in h.tg.last_buttons(CUSTOMER)]
    assert "شدات ببجي فوري ID" in names and "PUBG Global ID UC" not in names
    await h.press(CUSTOMER, f"k:{key}:0")
    assert "<b>شدات ببجي فوري ID</b>" in h.tg.last_text(CUSTOMER)
    assert any(b["text"].startswith("60 شده — ") for b in h.tg.last_buttons(CUSTOMER))

    await h.press(CUSTOMER, "p:18")
    assert "<b>60 شده</b>" in h.tg.last_text(CUSTOMER)
    await h.text(CUSTOMER, "5123456789")
    assert "المنتج: 60 شده" in h.tg.last_text(CUSTOMER)
    confirm = next(b for b in h.tg.last_buttons(CUSTOMER) if b["callback_data"].startswith("o:"))
    await h.press(CUSTOMER, confirm["callback_data"])
    assert "<b>60 شده</b> × 1" in h.tg.last_text(CUSTOMER)
    [order] = await h.svc.repo.orders_for_user(CUSTOMER)
    assert order.product_name == "60 شده"

    # "-" goes back to the provider's names
    await h.press(OWNER, f"a:cn:{key}:0")
    await h.text(OWNER, "-")
    await h.press(OWNER, f"a:pn:{key}:0:18")
    await h.text(OWNER, "-")
    await h.text(CUSTOMER, "🛒 المنتجات")
    assert "PUBG Global ID UC" in [b["text"] for b in h.tg.last_buttons(CUSTOMER)]
    assert await h.svc.repo.get_settings() and \
        (await h.svc.repo.get_settings()).get("category_names") == {}
    logs = [a for _, _, a, _ in await h.svc.repo.owner_logs()]
    assert "rename_category" in logs and "rename_product" in logs


async def test_rename_rejects_bad_names_and_customers(h):
    key = await _pubg_key(h)
    await h.press(OWNER, f"a:cn:{key}:0")
    await h.text(OWNER, "x" * 61)
    assert "اسم غير صالح" in h.tg.last_text(OWNER)
    await h.press(OWNER, "a:cx")

    await h.text(CUSTOMER, "/start")
    await h.press(CUSTOMER, f"a:cn:{key}:0")
    await h.text(CUSTOMER, "hacked")
    await h.press(CUSTOMER, f"a:pn:{key}:0:18")
    await h.text(CUSTOMER, "hacked")
    settings = await h.svc.repo.get_settings()
    assert not settings.get("category_names") and not settings.get("product_names")
