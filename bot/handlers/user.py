"""Customer-facing handlers: catalog, ordering, wallet, history."""
from __future__ import annotations

import logging
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, ContextTypes

from ..g4s import G4SError, G4SUnavailable, Product
from ..money import ZERO, to_decimal
from ..orders import PLAYER_BLOCKED_TEXT, OrderRejectedBeforeDebit, PlayerBlocked, PriceChanged
from ..repo import InsufficientFunds, UserBanned
from . import owner
from .common import (BTN_BALANCE, BTN_LEDGER, BTN_ORDERS, BTN_PRODUCTS, BTN_TOPUP, LEDGER_LABELS,
                     STATUS_LABELS, esc, field_label, main_keyboard, order_text, reply, svc, when)

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
    name = esc(update.effective_user.first_name or "")
    await reply(update, f"أهلاً {name} 👋\n🆔: <code>{update.effective_user.id}</code>\n\n"
                        "اختر من القائمة تحت.",
                reply_markup=main_keyboard(s.is_owner(update.effective_user.id)))


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    user = await s.repo.get_user(update.effective_user.id)
    await reply(update, f"💰 رصيدك: <b>{s.money(user.balance)}</b>")


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
    await reply(update, text)


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    orders = await s.repo.orders_for_user(update.effective_user.id, limit=10)
    if not orders:
        await reply(update, "ما عندك طلبات للحين.")
        return
    rows = [[InlineKeyboardButton(
        f"{STATUS_LABELS.get(o.status, o.status)[:2]} {o.product_name[:28]} — {s.money(o.amount)}",
        callback_data=f"od:{o.uuid}")] for o in orders]
    await reply(update, "📦 آخر طلباتك:", reply_markup=InlineKeyboardMarkup(rows))


async def order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    q = update.callback_query
    await q.answer()
    order = await s.repo.get_order(q.data.split(":", 1)[1])
    if order is None or order.user_hash != s.repo.user_hash(update.effective_user.id):
        await reply(update, "الطلب غير موجود.")
        return
    await reply(update, order_text(order, s, detailed=True))


async def ledger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    entries = await s.repo.ledger_for_user(update.effective_user.id, limit=10)
    if not entries:
        await reply(update, "ما فيه عمليات على محفظتك للحين.")
        return
    lines = ["🧾 <b>آخر العمليات</b>"]
    for e in entries:
        sign = "+" if e.amount > 0 else ""
        lines.append(f"{when(e.created_at)} | {LEDGER_LABELS.get(e.type, e.type)} | "
                     f"{sign}{s.money(e.amount)} | الرصيد: {s.money(e.balance_after)}")
    await reply(update, "\n".join(lines))


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
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except BadRequest:
            pass
    await reply(update, text, reply_markup=markup)


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    s = svc(context)
    if update.callback_query:
        await update.callback_query.answer()
        page = int(update.callback_query.data.split(":")[1])
    context.user_data.pop("draft", None)
    try:
        cats = await s.catalog.categories(await s.runtime.visible_products())
    except (G4SError, G4SUnavailable) as exc:
        log.warning("Catalog load failed: %s", exc)
        await reply(update, "تعذّر تحميل المنتجات الحين، جرّب بعد شوي.")
        return
    if not cats:
        await reply(update, "ما فيه منتجات متاحة حالياً.")
        return
    named = sorted([(await s.runtime.category_name(c), c) for c in cats], key=lambda nc: nc[0])
    chunk = named[page * PAGE:(page + 1) * PAGE]
    rows = [[InlineKeyboardButton(name[:60], callback_data=f"k:{c.key}:0")] for name, c in chunk]
    if nav := _pager("c", page, len(cats)):
        rows.append(nav)
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
    rows.append([InlineKeyboardButton("🔙 الأقسام", callback_data="c:0")])
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
        text += "🔒 رصيدك ما يكفي لهذا المنتج، اشحن من «➕ شحن الرصيد».\n"
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
        await q.edit_message_text("تم إلغاء الطلب.")
    except BadRequest:
        pass


async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free text: either a menu button or an answer for the current draft."""
    text = (update.effective_message.text or "").strip()
    routes = {BTN_PRODUCTS: categories, BTN_BALANCE: balance, BTN_ORDERS: my_orders,
              BTN_LEDGER: ledger, BTN_TOPUP: topup}
    if text in routes:
        context.user_data.pop("owner_step", None)  # a menu tap cancels an owner prompt
        await routes[text](update, context)
        return
    if await owner.handle_text(update, context):
        return

    s = svc(context)
    draft = context.user_data.get("draft")
    if not draft or draft["uuid"]:
        await reply(update, "اختر من القائمة 👇",
                    reply_markup=main_keyboard(s.is_owner(update.effective_user.id)))
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
                            f"المطلوب: {s.money(exc.needed)}\nاضغط «➕ شحن الرصيد».")
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
        await reply(update, "الطلب منفّذ مسبقاً:\n" + order_text(order, s))
    elif order.status == "accept":
        await reply(update, "✅ تم تنفيذ طلبك بنجاح\n\n" + order_text(order, s, detailed=True))
    elif order.status == "reject":
        await reply(update, "❌ الطلب انرفض، ورجع المبلغ لمحفظتك.\n\n" + order_text(order, s))
    else:
        await reply(update, "⏳ طلبك قيد التنفيذ، بنرسل لك أول ما يكتمل.\n\n" + order_text(order, s))


async def _player_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE, player_id: str,
                          product_name: str) -> None:
    """Customer entered a blocked player ID: cancel, tell them, and warn the owner."""
    s = svc(context)
    tg_id = update.effective_user.id
    await reply(update, PLAYER_BLOCKED_TEXT)
    await s.repo.log_user(tg_id, "blocked_player_attempt", f"{player_id} {product_name}")
    await s.orders.alert_owner(
        "⚠️ عميل حاول يشحن لآيدي محظور\n"
        f"العميل: {tg_id}\nالآيدي: {player_id}\nالمنتج: {product_name}")
