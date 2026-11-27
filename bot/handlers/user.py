"""Customer-facing handlers: catalog, ordering, wallet, history."""
from __future__ import annotations

import logging
import secrets
import time
import uuid
from datetime import timedelta
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, ContextTypes

from ..g4s import G4SError, G4SUnavailable, Product
from ..money import ZERO, InvalidAmount, parse_user_amount, to_decimal
from ..orders import PLAYER_BLOCKED_TEXT, OrderRejectedBeforeDebit, PlayerBlocked, PriceChanged
from ..repo import (InsufficientFunds, TransferLimit, TransferNotAllowed, UserBanned,
                    utcnow)
from . import owner
from .common import (ACCOUNT_ROW, BTN_ACCOUNT, BTN_TRANSFER, BTN_BALANCE, BTN_LEDGER, BTN_ORDERS, BTN_PRODUCTS, BTN_TOPUP, HOME_ROW,
                     LEDGER_LABELS, STATUS_LABELS, TRANSFER_KINDS, TOPUP_BUTTON, bottom_keyboard, esc,
                     field_label, home_markup, menu_markup, menu_text, order_text, reply, svc, when)

log = logging.getLogger(__name__)
PAGE = 10
MAX_PARAM_LEN = 120


# ---------------------------------------------------------------------------
# Gate: runs before every handler (group -1)
# ---------------------------------------------------------------------------
async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.is_bot:
        raise ApplicationHandlerStop
    if update.effective_chat and update.effective_chat.type != "private":
        raise ApplicationHandlerStop
    s = svc(context)
    owner = s.is_owner(user.id)

    if not owner and not s.action_limiter.allow(user.id):
        if update.callback_query:
            await update.callback_query.answer("لحظة، طلبات كثيرة. جرّب بعد دقيقة.", show_alert=True)
        raise ApplicationHandlerStop

    banned = s.cached_user(user.id)
    if banned is None:
        record, _ = await s.repo.ensure_user(user.id, user.username, user.first_name)
        banned = record.banned
        s.cache_user(user.id, banned)
    if banned and not owner:
        if update.callback_query:
            await update.callback_query.answer()
        raise ApplicationHandlerStop

    if not owner and await s.runtime.maintenance():
        if update.callback_query:
            await update.callback_query.answer("البوت تحت الصيانة حالياً 🛠", show_alert=True)
        else:
            await reply(update, "🛠 البوت تحت الصيانة حالياً، نرجع قريب إن شاء الله.")
        raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    context.user_data.pop("draft", None)
    _clear_transfer(context)
    await _set_bottom_keyboard(update, s.is_owner(update.effective_user.id))
    await reply(update, menu_text(update.effective_user), reply_markup=menu_markup())


async def _set_bottom_keyboard(update: Update, is_owner: bool) -> None:
    """Set the keyboard under the text box (owner: panel button; customers: none).

    A message can carry either this keyboard or inline buttons, not both, so it
    rides on a short helper message that is deleted right away; the keyboard
    change stays in effect."""
    try:
        helper = await update.effective_message.reply_text(
            "🔄", reply_markup=bottom_keyboard(is_owner))
        await helper.delete()
    except Exception as exc:  # cosmetic only; never block /start
        log.info("Bottom keyboard update failed: %s", exc)


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline main menu buttons (m:<item>)."""
    item = update.callback_query.data.split(":", 1)[1]
    if item != "xfer":
        _clear_transfer(context)
    if item == "shop":
        await categories(update, context)
        return
    if item == "xfer":
        await transfer_start(update, context)
        return
    await update.callback_query.answer()
    if item == "home":
        context.user_data.pop("draft", None)
        await _show(update, menu_text(update.effective_user, back=True), menu_markup())
    elif item == "acc":
        await account(update, context)
    elif item == "bal":
        await balance(update, context)
    elif item == "top":
        await topup(update, context)
    elif item == "ord":
        await my_orders(update, context)
    elif item == "led":
        await ledger(update, context)


async def account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    tg = update.effective_user
    user = await s.repo.get_user(tg.id)
    text = ("👤 <b>حسابي</b>\n\n"
            f"الاسم: {esc(tg.first_name or '')}\n"
            f"🆔: <code>{tg.id}</code>\n"
            f"💰 رصيدك: <b>{s.money(user.balance)}</b>\n"
            f"📅 تاريخ التسجيل: {when(user.created_at)[:10]}")
    b = InlineKeyboardButton
    await _show(update, text, InlineKeyboardMarkup([
        [b(BTN_ORDERS, callback_data="m:ord"), b(BTN_LEDGER, callback_data="m:led")],
        [b(BTN_TRANSFER, callback_data="m:xfer")],
        HOME_ROW,
    ]))


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    user = await s.repo.get_user(update.effective_user.id)
    await _show(update, f"💰 رصيدك: <b>{s.money(user.balance)}</b>", home_markup([TOPUP_BUTTON]))


async def topup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    contact = s.settings.support_contact
    text = (
        "➕ <b>شحن الرصيد</b>\n"
        "الشحن حالياً يدوي: يتم شحن محفظتك واستخدام رصيدك أي وقت.\n\n"
        "<b>طرق الدفع</b>\n"
        "داخل السعودية: راجحي، برق، STC Bank، رابط دفع محلي\n"
        "خارج السعودية: رابط دفع دولي، جنيه مصري، USDT\n\n"
        f"ايديك تيليجرام: <code>{update.effective_user.id}</code>"
    )
    if contact:
        text += f"\nطلب شحن محفظة: {esc(contact)}"
    await _show(update, text, home_markup())


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    orders = await s.repo.orders_for_user(update.effective_user.id, limit=10)
    if not orders:
        await _show(update, "ما عندك طلبات للحين.", InlineKeyboardMarkup([
            [InlineKeyboardButton(BTN_PRODUCTS, callback_data="m:shop")], ACCOUNT_ROW]))
        return
    rows = [[InlineKeyboardButton(
        f"{STATUS_LABELS.get(o.status, o.status)[:2]} {o.product_name[:28]} — {s.money(o.amount)}",
        callback_data=f"od:{o.uuid}")] for o in orders]
    await _show(update, "📦 آخر طلباتك:", InlineKeyboardMarkup([*rows, ACCOUNT_ROW]))


async def order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    q = update.callback_query
    await q.answer()
    order = await s.repo.get_order(q.data.split(":", 1)[1])
    if order is None or order.user_hash != s.repo.user_hash(update.effective_user.id):
        await reply(update, "الطلب غير موجود.")
        return
    await _show(update, order_text(order, s, detailed=True),
                home_markup([InlineKeyboardButton("🔙 طلباتي", callback_data="m:ord")]))


async def ledger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    entries = await s.repo.ledger_for_user(update.effective_user.id, limit=10)
    if not entries:
        await _show(update, "ما فيه عمليات على محفظتك للحين.", InlineKeyboardMarkup([ACCOUNT_ROW]))
        return
    lines = ["🧾 <b>آخر العمليات</b>"]
    for e in entries:
        sign = "+" if e.amount > 0 else ""
        label = LEDGER_LABELS.get(e.type, e.type)
        if e.type in TRANSFER_KINDS and e.note:
            label = f"{label} {esc(e.note)}"
        lines.append(f"{when(e.created_at)} | {label} | "
                     f"{sign}{s.money(e.amount)} | الرصيد: {s.money(e.balance_after)}")
    await _show(update, "\n".join(lines), InlineKeyboardMarkup([ACCOUNT_ROW]))


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def _pager(prefix: str, page: int, total: int) -> list[InlineKeyboardButton]:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"{prefix}:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"{prefix}:{page + 1}"))
    return nav


async def _show(update: Update, text: str, markup: InlineKeyboardMarkup) -> None:
    """Edit the tapped message in place; send a new one for typed input."""
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return  # tapped the screen it is already on
    await reply(update, text, reply_markup=markup)


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    s = svc(context)
    q = update.callback_query
    if q:
        await q.answer()
        if q.data.startswith("c:"):
            page = int(q.data.split(":")[1])
    context.user_data.pop("draft", None)
    try:
        cats = await s.catalog.categories(await s.runtime.visible_products())
    except (G4SError, G4SUnavailable) as exc:
        log.warning("Catalog load failed: %s", exc)
        await reply(update, "تعذّر تحميل المنتجات الحين، جرّب بعد شوي.")
        return
    if not cats:
        await _show(update, "ما فيه منتجات متاحة حالياً.", home_markup())
        return
    named = sorted([(await s.runtime.category_name(c), c) for c in cats], key=lambda nc: nc[0])
    chunk = named[page * PAGE:(page + 1) * PAGE]
    rows = [[InlineKeyboardButton(name[:60], callback_data=f"k:{c.key}:0")] for name, c in chunk]
    if nav := _pager("c", page, len(cats)):
        rows.append(nav)
    rows.append(HOME_ROW)
    await _show(update, "🛒 اختر القسم:", InlineKeyboardMarkup(rows))


def _min_qty(p: Product) -> int:
    return p.qty.choices[0] if p.qty.kind == "choices" else p.qty.min


async def _balance(s, telegram_id: int):
    user = await s.repo.get_user(telegram_id)
    return user.balance if user else ZERO


def _product_rows(items, key: str, page: int, total: int,
                  use_disabled: bool) -> list[list[InlineKeyboardButton]]:
    """items: (product, label, locked). A locked product (balance below its price)
    gets a 🔒 and, when use_disabled, Telegram's disabled button (grey, does
    nothing; Bot API 10.3). Without it, tapping shows the "balance too low" alert."""
    rows = []
    for p, label, locked in items:
        if locked and use_disabled:
            button = InlineKeyboardButton(f"🔒 {label}", api_kwargs={"disabled": {}})
        elif locked:
            button = InlineKeyboardButton(f"🔒 {label}", callback_data=f"p:{p.id}")
        else:
            button = InlineKeyboardButton(label, callback_data=f"p:{p.id}")
        rows.append([button])
    if nav := _pager(f"k:{key}", page, total):
        rows.append(nav)
    if any(locked for _, _, locked in items):
        rows.append([TOPUP_BUTTON])
    rows.append([InlineKeyboardButton("🔙 الأقسام", callback_data="c:0"), *HOME_ROW])
    return rows


async def category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    q = update.callback_query
    await q.answer()
    _, key, page = q.data.split(":")
    page = int(page)
    try:
        cat = await s.catalog.category(key, await s.runtime.visible_products())
    except (G4SError, G4SUnavailable) as exc:
        log.warning("Catalog load failed: %s", exc)
        await reply(update, "تعذّر تحميل المنتجات الحين، جرّب بعد شوي.")
        return
    if cat is None:
        await categories(update, context)
        return
    balance = await _balance(s, update.effective_user.id)
    items = []
    for p in cat.products[page * PAGE:(page + 1) * PAGE]:
        price = await s.runtime.price(p, _min_qty(p))
        prefix = "من " if p.qty.kind != "one" else ""
        name = await s.runtime.product_name(p)
        items.append((p, f"{name[:40]} — {prefix}{s.money(price)}", balance < price))
    text = (f"<b>{esc(await s.runtime.category_name(cat))}</b>\n"
            f"رصيدك: {s.money(balance)}\n")
    if any(locked for _, _, locked in items):
        text += ("🔒 إذا ظهر لك قفل على منتج يعني رصيدك ما يكفي لهذا المنتج،\n"
                 "اضغط زر «➕ شحن الرصيد» بالأسفل.\n")
    text += "اختر المنتج:"
    try:
        await _show(update, text, InlineKeyboardMarkup(
            _product_rows(items, key, page, len(cat.products), use_disabled=True)))
    except BadRequest as exc:
        # Telegram refused the disabled buttons: send ordinary 🔒 buttons instead.
        log.warning("Disabled buttons refused, falling back: %s", exc)
        await _show(update, text, InlineKeyboardMarkup(
            _product_rows(items, key, page, len(cat.products), use_disabled=False)))


async def product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    q = update.callback_query
    p = await s.catalog.product(int(q.data.split(":")[1]))
    if p is None or not p.available or not await s.runtime.is_visible(p.id):
        await q.answer()
        await reply(update, "المنتج غير متوفر حالياً.")
        return
    price = await s.runtime.price(p, _min_qty(p))
    balance = await _balance(s, update.effective_user.id)
    if balance < price:
        await q.answer(f"🔒 رصيدك ما يكفي\nرصيدك: {s.money(balance)}\n"
                       f"السعر: {s.money(price)}\nاشحن من «➕ شحن الرصيد»", show_alert=True)
        return
    await q.answer()
    _clear_transfer(context)
    context.user_data["draft"] = {"product_id": p.id, "values": [], "qty": None,
                                  "uuid": None, "total": None}
    await _next_step(update, context, p)


async def _next_step(update: Update, context: ContextTypes.DEFAULT_TYPE, p: Product) -> None:
    s = svc(context)
    draft = context.user_data["draft"]
    if len(draft["values"]) < len(p.params):
        index = len(draft["values"])
        label = field_label(p.params[index], index)
        await reply(update, f"<b>{esc(await s.runtime.product_name(p))}</b>\n\n✍️ {esc(label)}:")
        return
    if draft["qty"] is None:
        if p.qty.kind == "one":
            draft["qty"] = 1
        elif p.qty.kind == "choices":
            buttons = [InlineKeyboardButton(str(n), callback_data=f"q:{n}") for n in p.qty.choices[:30]]
            rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
            await reply(update, "اختر الكمية:", reply_markup=InlineKeyboardMarkup(rows))
            return
        else:
            unit = await s.runtime.price(p, p.qty.min)
            await reply(update, f"اكتب الكمية (من {p.qty.min} إلى {p.qty.max}).\n"
                                f"السعر لكمية {p.qty.min}: {s.money(unit)}")
            return
    await _confirm(update, context, p)


async def _confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, p: Product) -> None:
    s = svc(context)
    draft = context.user_data["draft"]
    if draft["total"] is None:
        draft["total"] = str(await s.runtime.price(p, draft["qty"]))
    if draft["uuid"] is None:
        draft["uuid"] = str(uuid.uuid4())
    user = await s.repo.get_user(update.effective_user.id)
    lines = ["🧾 <b>تأكيد الطلب</b>", f"المنتج: {esc(await s.runtime.product_name(p))}",
             f"الكمية: {draft['qty']}"]
    for index, (label, value) in enumerate(zip(p.params, draft["values"])):
        lines.append(f"{esc(field_label(label, index))}: <code>{esc(value)}</code>")
    lines += [f"الإجمالي: <b>{s.money(to_decimal(draft['total']))}</b>",
              f"رصيدك: {s.money(user.balance)}",
              "\nتأكد من البيانات، الطلب ما يتلغى بعد التنفيذ."]
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأكيد", callback_data=f"o:{draft['uuid']}"),
        InlineKeyboardButton("❌ إلغاء", callback_data="x"),
    ]])
    await reply(update, "\n".join(lines), reply_markup=markup)


async def choose_qty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    q = update.callback_query
    await q.answer()
    draft = context.user_data.get("draft")
    if not draft or draft["qty"] is not None:
        return
    p = await s.catalog.product(draft["product_id"])
    qty = int(q.data.split(":")[1])
    if p is None or p.qty.validate(qty):
        await reply(update, "كمية غير صالحة.")
        return
    draft["qty"] = qty
    await _confirm(update, context, p)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    context.user_data.pop("draft", None)
    try:
        await q.edit_message_text("تم إلغاء الطلب.", reply_markup=home_markup())
    except BadRequest:
        pass


async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free text: either a menu button or an answer for the current draft."""
    text = (update.effective_message.text or "").strip()
    routes = {BTN_PRODUCTS: categories, BTN_BALANCE: balance, BTN_ORDERS: my_orders,
              BTN_LEDGER: ledger, BTN_TOPUP: topup, BTN_ACCOUNT: account}
    if text in routes:
        context.user_data.pop("owner_step", None)  # a menu tap cancels an owner prompt
        await routes[text](update, context)
        return
    if await owner.handle_text(update, context):
        return
    if await transfer_text(update, context):
        return

    s = svc(context)
    draft = context.user_data.get("draft")
    if not draft or draft["uuid"]:
        await reply(update, menu_text(update.effective_user, back=True), reply_markup=menu_markup())
        return
    p = await s.catalog.product(draft["product_id"])
    if p is None or not await s.runtime.is_visible(p.id):
        context.user_data.pop("draft", None)
        await reply(update, "المنتج غير متوفر حالياً.")
        return

    if len(draft["values"]) < len(p.params):
        if not text or len(text) > MAX_PARAM_LEN or "\n" in text:
            await reply(update, f"قيمة غير صالحة (بحد أقصى {MAX_PARAM_LEN} حرف وسطر واحد).")
            return
        if await s.repo.is_player_blocked(text):
            context.user_data.pop("draft", None)
            await _player_blocked(update, context, text, p.name)
            return
        draft["values"].append(text)
        await _next_step(update, context, p)
        return

    if draft["qty"] is None and p.qty.kind == "range":
        digits = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        if not digits.isdigit():
            await reply(update, "اكتب الكمية أرقام فقط.")
            return
        qty = int(digits)
        if (err := p.qty.validate(qty)):
            await reply(update, err)
            return
        draft["qty"] = qty
        await _confirm(update, context, p)
        return

    await reply(update, "اختر من الأزرار فوق 👆")


# ---------------------------------------------------------------------------
# Placing the order
# ---------------------------------------------------------------------------
async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    q = update.callback_query
    tg_id = update.effective_user.id
    order_uuid = q.data.split(":", 1)[1]
    draft = context.user_data.get("draft")

    # Double tap on an order that was already placed: show it, never re-charge.
    existing = await s.repo.get_order(order_uuid)
    if existing and existing.user_hash == s.repo.user_hash(tg_id):
        await q.answer("الطلب منفّذ مسبقاً")
        await reply(update, order_text(existing, s))
        return
    if not draft or draft.get("uuid") != order_uuid:
        await q.answer("انتهت صلاحية الطلب، ابدأ من جديد.", show_alert=True)
        return
    if not s.is_owner(tg_id) and not s.order_limiter.allow(tg_id):
        await q.answer("طلبات كثيرة، انتظر دقيقة.", show_alert=True)
        return
    await q.answer()
    try:
        await q.edit_message_reply_markup(None)
    except BadRequest:
        pass
    await reply(update, "⏳ جاري تنفيذ طلبك...")

    try:
        result = await s.orders.place(tg_id, order_uuid, draft["product_id"], draft["qty"],
                                      draft["values"], to_decimal(draft["total"]))
    except PriceChanged as exc:
        draft["total"] = str(exc.new_total)
        p = await s.catalog.product(draft["product_id"])
        await reply(update, "⚠️ السعر تغيّر، راجع الإجمالي الجديد:")
        await _confirm(update, context, p)
        return
    except InsufficientFunds as exc:
        await reply(update, f"رصيدك ما يكفي.\nرصيدك: {s.money(exc.balance)}\n"
                            f"المطلوب: {s.money(exc.needed)}\nاضغط «➕ شحن الرصيد».",
                    reply_markup=home_markup([TOPUP_BUTTON]))
        return
    except PlayerBlocked as exc:
        context.user_data.pop("draft", None)
        p = await s.catalog.product(draft["product_id"])
        await _player_blocked(update, context, exc.value, p.name if p else "")
        return
    except OrderRejectedBeforeDebit as exc:
        context.user_data.pop("draft", None)
        await reply(update, f"تعذّر تنفيذ الطلب: {esc(exc)}")
        return
    except UserBanned:
        return

    context.user_data.pop("draft", None)
    order = result.order
    if result.duplicate:
        await reply(update, "الطلب منفّذ مسبقاً:\n" + order_text(order, s),
                    reply_markup=home_markup())
    elif order.status == "accept":
        await reply(update, "✅ تم تنفيذ طلبك بنجاح\n\n" + order_text(order, s, detailed=True),
                    reply_markup=home_markup())
    elif order.status == "reject":
        await reply(update, "❌ الطلب انرفض، ورجع المبلغ لمحفظتك.\n\n" + order_text(order, s),
                    reply_markup=home_markup())
    else:
        await reply(update, "⏳ طلبك قيد التنفيذ، بنرسل لك أول ما يكتمل.\n\n" + order_text(order, s),
                    reply_markup=home_markup())


async def _player_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE, player_id: str,
                          product_name: str) -> None:
    """Customer entered a blocked player ID: cancel, tell them, and warn the owner."""
    s = svc(context)
    tg_id = update.effective_user.id
    await reply(update, PLAYER_BLOCKED_TEXT, reply_markup=home_markup())
    await s.repo.log_user(tg_id, "blocked_player_attempt", f"{player_id} {product_name}")
    await s.orders.alert_owner(
        "⚠️ عميل حاول يشحن لآيدي محظور\n"
        f"العميل: {tg_id}\nالآيدي: {player_id}\nالمنتج: {product_name}")



# ---------------------------------------------------------------------------
# Transfer to a friend (needs the owner's approval)
#   user_data["xfer"]    = {"step": "id"|"amount", "at": t, "to": id, "name": str}
#   user_data["xfer_op"] = {"id": nonce, "tid": transfer id, "to", "name", "amount"}
# ---------------------------------------------------------------------------
XFER_TTL_SECONDS = 600
XFER_CANCEL = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="t:x")]])
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
PENDING_LINE = "⏳ بانتظار موافقة المالك"


def _riyadh_day() -> str:
    return (utcnow() + timedelta(hours=3)).date().isoformat()


def _clear_transfer(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("xfer", None)
    context.user_data.pop("xfer_op", None)


async def transfer_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    q = update.callback_query
    tg_id = update.effective_user.id
    if not await s.runtime.transfers_enabled():
        await q.answer("🔜 التحويل لصديق متوقف حالياً.", show_alert=True)
        return
    await q.answer()
    cfg = s.settings
    balance = await _balance(s, tg_id)
    if balance < cfg.transfer_min:
        await _show(update, f"🔄 <b>{BTN_TRANSFER[2:]}</b>\n\nرصيدك: {s.money(balance)}\n"
                            f"أقل مبلغ للتحويل {s.money(cfg.transfer_min)}، ورصيدك ما يكفي.",
                    home_markup([TOPUP_BUTTON]))
        return
    if await s.repo.transfers_today(tg_id, _riyadh_day()) >= cfg.transfers_per_day:
        await _show(update, f"وصلت حد التحويلات اليومي ({cfg.transfers_per_day}). جرّب بكرة.",
                    home_markup())
        return
    context.user_data.pop("draft", None)
    context.user_data.pop("xfer_op", None)
    context.user_data["xfer"] = {"step": "id", "at": time.monotonic()}
    await _show(update, f"🔄 <b>{BTN_TRANSFER[2:]}</b>\n\n"
                        f"رصيدك: {s.money(balance)}\n"
                        f"أقل تحويل: {s.money(cfg.transfer_min)} · "
                        f"أعلى تحويل: {s.money(cfg.transfer_max)}\n"
                        "التحويل يوصل لصديقك بعد موافقة المالك.\n\n"
                        "أرسل آيدي صديقك (يلقاه عنده في «👤 حسابي»):", XFER_CANCEL)


async def transfer_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Answers to the transfer prompts. Returns True if the text was one."""
    step = context.user_data.get("xfer")
    if not step:
        return False
    if time.monotonic() - step["at"] > XFER_TTL_SECONDS:
        _clear_transfer(context)
        return False
    s = svc(context)
    cfg = s.settings
    tg_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()

    if step["step"] == "id":
        digits = text.translate(_AR_DIGITS)
        if not digits.isdigit():
            await reply(update, "الآيدي لازم يكون أرقام بس. أرسله مرة ثانية:",
                        reply_markup=XFER_CANCEL)
            return True
        to_id = int(digits)
        if to_id == tg_id:
            await reply(update, "ما تقدر تحوّل لنفسك 😅\nأرسل آيدي صديقك:", reply_markup=XFER_CANCEL)
            return True
        target = await s.repo.get_user(to_id)
        if target is None:
            await reply(update, "هذا الآيدي غير مسجّل في البوت. تأكد منه وأرسله مرة ثانية:",
                        reply_markup=XFER_CANCEL)
            return True
        if target.banned:
            await reply(update, "ما تقدر تحوّل لهذا الحساب. أرسل آيدي ثاني:",
                        reply_markup=XFER_CANCEL)
            return True
        name = target.first_name or str(to_id)
        step.update(step="amount", to=to_id, name=name, at=time.monotonic())
        await reply(update, f"🔄 تحويل إلى: <b>{esc(name)}</b>\n🆔: <code>{to_id}</code>\n\n"
                            f"رصيدك: {s.money(await _balance(s, tg_id))}\n"
                            "أرسل المبلغ اللي تبي تحوّله:", reply_markup=XFER_CANCEL)
        return True

    # step == "amount"
    try:
        amount = parse_user_amount(text.split()[0] if text else "")
    except InvalidAmount:
        await reply(update, "مبلغ غير صالح. اكتب رقم، مثل <code>20</code>:", reply_markup=XFER_CANCEL)
        return True
    if amount < cfg.transfer_min:
        await reply(update, f"أقل مبلغ للتحويل {s.money(cfg.transfer_min)}", reply_markup=XFER_CANCEL)
        return True
    if amount > cfg.transfer_max:
        await reply(update, f"أعلى مبلغ للتحويل {s.money(cfg.transfer_max)}", reply_markup=XFER_CANCEL)
        return True
    balance = await _balance(s, tg_id)
    if balance < amount:
        await reply(update, f"رصيدك ما يكفي. رصيدك {s.money(balance)}\nأرسل مبلغ أقل:",
                    reply_markup=XFER_CANCEL)
        return True
    nonce = secrets.token_hex(4)
    context.user_data.pop("xfer", None)
    context.user_data["xfer_op"] = {"id": nonce, "tid": uuid.uuid4().hex, "to": step["to"],
                                    "name": step["name"], "amount": str(amount)}
    await reply(update, "⚠️ <b>تأكيد التحويل</b>\n\n"
                        f"إلى: {esc(step['name'])} (<code>{step['to']}</code>)\n"
                        f"المبلغ: <b>{s.money(amount)}</b>\n"
                        f"رصيدك بعد التحويل: {s.money(balance - amount)}\n\n"
                        "تأكد من الاسم، التحويل ما يتلغى بعد التأكيد.\n"
                        "يوصل لصديقك بعد موافقة المالك.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ تأكيد التحويل", callback_data=f"t:ok:{nonce}"),
                    InlineKeyboardButton("❌ إلغاء", callback_data="t:x")]]))
    return True


async def transfer_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """t:x (cancel) and t:ok:<nonce> (confirm)."""
    s = svc(context)
    q = update.callback_query
    if q.data == "t:x":
        _clear_transfer(context)
        await q.answer()
        await _show(update, "تم إلغاء التحويل.", home_markup())
        return
    op = context.user_data.get("xfer_op")
    if not op or op["id"] != q.data[len("t:ok:"):]:
        await q.answer("انتهت صلاحية التحويل، ابدأ من جديد.", show_alert=True)
        return
    context.user_data.pop("xfer_op", None)  # before any await: a double tap finds nothing
    if not await s.runtime.transfers_enabled():
        await q.answer("🔜 التحويل لصديق متوقف حالياً.", show_alert=True)
        return
    await q.answer()
    tg_id = update.effective_user.id
    amount = Decimal(op["amount"])
    try:
        transfer, new_balance, created = await s.repo.create_transfer(
            op["tid"], tg_id, op["to"], amount, _riyadh_day(), s.settings.transfers_per_day)
    except InsufficientFunds as exc:
        await _show(update, f"رصيدك ما يكفي. رصيدك {s.money(exc.balance)}",
                    home_markup([TOPUP_BUTTON]))
        return
    except TransferLimit:
        await _show(update, f"وصلت حد التحويلات اليومي ({s.settings.transfers_per_day}). "
                            "جرّب بكرة.", home_markup())
        return
    except (TransferNotAllowed, UserBanned):
        await _show(update, "ما تقدر تحوّل لهذا الحساب.", home_markup())
        return
    if not created:
        return
    sender_name = update.effective_user.first_name or str(tg_id)
    await s.repo.log_user(tg_id, "transfer_requested", f"{transfer.id} -> {op['to']} {amount}")
    await _show(update, f"✅ تم التحويل\n{PENDING_LINE}\n\n"
                        f"حوّلت {s.money(amount)} إلى {esc(op['name'])}\n"
                        f"رصيدك الحالي: {s.money(new_balance)}", home_markup())
    try:
        await context.bot.send_message(
            op["to"], f"💰 وصلك تحويل!\n{PENDING_LINE}\n\n"
                      f"من: {esc(sender_name)}\nالمبلغ: {s.money(amount)}\n"
                      "يوصل لمحفظتك أول ما يوافق المالك.",
            parse_mode="HTML", reply_markup=home_markup())
    except Exception as exc:
        log.info("Could not notify transfer receiver: %s", exc)
    await owner.notify_new_transfer(context, transfer, tg_id, sender_name, op["to"], op["name"])
