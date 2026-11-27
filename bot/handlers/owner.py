"""Owner tools. Every handler re-checks the caller's Telegram ID against
OWNER_IDS (from the environment) — hiding the button is not protection."""
from __future__ import annotations

import functools
import logging
import re
import secrets
import time
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..g4s import G4SError, G4SUnavailable
from ..money import InvalidAmount, parse_user_amount, sell_total, to_decimal
from ..repo import InsufficientFunds, UserNotFound
from .common import esc, order_text, reply, svc, when

log = logging.getLogger(__name__)
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
CAT_KEY_RE = re.compile(r"^[0-9a-f]{10}$")
PV_PAGE = 10

COMMANDS_HELP = """<b>الأوامر</b> (اختيارية، الأزرار تسوي نفس الشي)
<code>/credit ID المبلغ [ملاحظة]</code> · <code>/debit ID المبلغ</code>
<code>/user ID</code> · <code>/ban ID</code> · <code>/unban ID</code>
<code>/products</code> · <code>/margin 10</code> · <code>/maintenance on|off</code>
<code>/order UUID</code> · <code>/refund UUID</code> · <code>/recheck</code> · <code>/logs</code>"""

# How long a "send me the ID / amount" prompt stays active.
STEP_TTL_SECONDS = 600
MARGIN_PRESETS = ("5", "8", "10", "12", "15", "20", "25", "30")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")



def owner_only(fn):
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or not svc(context).is_owner(user.id):
            if update.callback_query:
                await update.callback_query.answer()
            return None
        return await fn(update, context)
    return wrapper


def _parse_tg_id(raw: str) -> int:
    raw = raw.strip().translate(_AR_DIGITS)
    if not raw.isdigit():
        raise ValueError
    return int(raw)


async def _audit(context, update: Update, action: str, details: str) -> None:
    await svc(context).repo.log_owner(update.effective_user.id, action, details)


def _panel_markup(maintenance: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup([
        [b("💰 شحن رصيد عميل", callback_data="a:cr"), b("➖ خصم من عميل", callback_data="a:db")],
        [b("👤 إدارة عميل", callback_data="a:us"), b("🚫 آيديات محظورة", callback_data="a:bl")],
        [b("🧩 المنتجات المعروضة", callback_data="a:pv:0"), b("📈 نسبة الربح", callback_data="a:mg")],
        [b("📋 الطلبات المعلّقة", callback_data="a:open"), b("🔍 بحث عن طلب", callback_data="a:od")],
        [b("💳 رصيدي عند المزود", callback_data="a:bal"),
         b("🔄 تحقق من المعلّقة", callback_data="a:rc")],
        [b("🟢 إيقاف الصيانة" if maintenance else "🔴 تشغيل الصيانة", callback_data="a:mt"),
         b("📜 سجل المالك", callback_data="a:logs")],
        [b("❔ الأوامر", callback_data="a:help")],
    ])


CANCEL_MARKUP = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="a:cx")]])


@owner_only
async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    _clear_step(context)
    maintenance = await s.runtime.maintenance()
    margin = await s.runtime.margin()
    status = "🔴 مفعّلة (البيع متوقف)" if maintenance else "🟢 البيع شغّال"
    shown = len(await s.runtime.visible_products())
    await reply(update, "🛠 <b>لوحة المالك</b>\n\n"
                        f"الحالة: {status}\nنسبة الربح: {margin}%\n"
                        f"المنتجات المعروضة للعملاء: {shown}\n\nاختر من الأزرار:",
                reply_markup=_panel_markup(maintenance))


@owner_only
async def panel_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action in {"pv", "pk", "pt", "pa"}:
        await _products_button(update, context, action, parts[2:])
        return
    arg = parts[2] if len(parts) > 2 else ""
    if action in _BUTTONS:
        await _BUTTONS[action](update, context, arg)
        return
    if action == "bal":
        await provider_balance(update, context)
    elif action == "open":
        await open_orders(update, context)
    elif action == "mt":
        s = svc(context)
        new = not await s.runtime.maintenance()
        await _set_maintenance(update, context, new)
        await q.edit_message_reply_markup(_panel_markup(new))
    elif action == "logs":
        await logs(update, context)
    elif action == "rf" and UUID_RE.match(arg):
        await _do_refund(update, context, arg)


async def provider_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    try:
        bal, _ = await s.api.profile()
    except (G4SError, G4SUnavailable) as exc:
        await reply(update, f"تعذّر جلب الرصيد: {esc(exc)}")
        return
    await reply(update, f"💳 رصيدك عند game4station: <b>{bal}</b>")


async def open_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    orders = await s.repo.open_orders(limit=30)
    if not orders:
        await reply(update, "ما فيه طلبات معلّقة ✅")
        return
    rows = [[InlineKeyboardButton(
        f"{o.product_name[:25]} — {o.status} — {when(o.created_at)}",
        callback_data=f"a:oi:{o.uuid}")] for o in orders]
    await reply(update, f"📋 <b>الطلبات المعلّقة ({len(orders)})</b>\n"
                        "اضغط على الطلب لتفاصيله أو لاسترجاع المبلغ:",
                reply_markup=InlineKeyboardMarkup(rows))


async def _set_maintenance(update, context, on: bool) -> None:
    s = svc(context)
    await s.repo.update_settings(maintenance=on)
    s.runtime.invalidate()
    await _audit(context, update, "maintenance", "on" if on else "off")
    await reply(update, "🔴 تم تفعيل الصيانة، البيع متوقف." if on else "🟢 تم إيقاف الصيانة، البيع شغّال.")


@owner_only
async def maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0].lower() if context.args else "")
    if arg not in {"on", "off"}:
        await reply(update, "الاستخدام: <code>/maintenance on</code> أو <code>/maintenance off</code>")
        return
    await _set_maintenance(update, context, arg == "on")


@owner_only
async def margin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    if len(context.args) != 1:
        await reply(update, f"النسبة الحالية: {await s.runtime.margin()}%\nالاستخدام: <code>/margin 10</code>")
        return
    try:
        value = _parse_margin(context.args[0])
    except InvalidAmount:
        await reply(update, "نسبة غير صالحة (0 إلى 500).")
        return
    await _set_margin(update, context, value)


def _parse_margin(text: str) -> Decimal:
    value = to_decimal(text.strip().translate(_AR_DIGITS).rstrip("%").strip())
    if value < 0 or value > 500:
        raise InvalidAmount("margin out of range")
    return value


async def _set_margin(update: Update, context: ContextTypes.DEFAULT_TYPE, value: Decimal) -> None:
    s = svc(context)
    await s.repo.update_settings(margin_percent=str(value))
    s.runtime.invalidate()
    await _audit(context, update, "margin", str(value))
    await reply(update, f"✅ نسبة الربح صارت {value}%")


async def _adjust(update: Update, context: ContextTypes.DEFAULT_TYPE, sign: int) -> None:
    cmd = "credit" if sign > 0 else "debit"
    if len(context.args) < 2:
        await reply(update, f"الاستخدام: <code>/{cmd} ID المبلغ [ملاحظة]</code>")
        return
    try:
        tg_id = _parse_tg_id(context.args[0])
        amount = parse_user_amount(context.args[1])
    except (ValueError, InvalidAmount):
        await reply(update, "رقم العميل أو المبلغ غير صالح (المبلغ موجب وبحد أقصى خانتين عشريتين).")
        return
    note = " ".join(context.args[2:])[:200]
    await _apply_adjust(update, context, tg_id, amount, sign, note)


async def _apply_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_id: int,
                        amount: Decimal, sign: int, note: str) -> None:
    s = svc(context)
    cmd = "credit" if sign > 0 else "debit"
    kind = "owner_credit" if sign > 0 else "owner_debit"
    try:
        new_balance = await s.repo.adjust_balance(
            tg_id, amount * sign, kind, note=note, actor=s.repo.user_hash(update.effective_user.id))
    except UserNotFound:
        await reply(update, "العميل غير مسجّل. لازم يفتح البوت ويضغط /start أول.")
        return
    except InsufficientFunds as exc:
        await reply(update, f"رصيد العميل ما يكفي للخصم ({s.money(exc.balance)}).")
        return
    await _audit(context, update, cmd, f"{tg_id} {amount} {note}")
    await reply(update, f"✅ تم. رصيد العميل {tg_id} الجديد: {s.money(new_balance)}")
    verb = "إضافة" if sign > 0 else "خصم"
    try:
        await context.bot.send_message(
            tg_id, f"💰 تم {verb} {s.money(amount)} {'إلى' if sign > 0 else 'من'} رصيدك.\n"
                   f"رصيدك الحالي: {s.money(new_balance)}")
    except Exception as exc:  # user may have blocked the bot
        log.info("Could not notify %s: %s", tg_id, exc)


@owner_only
async def credit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _adjust(update, context, +1)


@owner_only
async def debit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _adjust(update, context, -1)


@owner_only
async def user_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    try:
        tg_id = _parse_tg_id(context.args[0])
    except (IndexError, ValueError):
        await reply(update, "الاستخدام: <code>/user ID</code>")
        return
    await _user_card(update, context, tg_id)


async def _user_card(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_id: int,
                     edit: bool = False) -> None:
    s = svc(context)
    user = await s.repo.get_user(tg_id)
    if user is None:
        await reply(update, "العميل غير مسجّل.")
        return
    orders = await s.repo.orders_for_user(tg_id, limit=5)
    lines = [
        f"👤 <b>{esc(user.first_name)}</b> @{esc(user.username or '-')}",
        f"ID: <code>{user.telegram_id}</code>",
        f"الرصيد: {s.money(user.balance)}",
        f"محظور: {'نعم' if user.banned else 'لا'}",
        f"التسجيل: {when(user.created_at)}",
        "\nآخر الطلبات:" if orders else "\nما عنده طلبات.",
    ]
    lines += [f"• {esc(o.product_name)} — {o.status} — {s.money(o.amount)} — <code>{o.uuid}</code>"
              for o in orders]
    b = InlineKeyboardButton
    markup = InlineKeyboardMarkup([
        [b("💰 شحن", callback_data=f"a:uc:{tg_id}"), b("➖ خصم", callback_data=f"a:ud:{tg_id}")],
        [b("✅ فك الحظر" if user.banned else "🚫 حظر", callback_data=f"a:ub:{tg_id}")],
        [b("🔙 لوحة المالك", callback_data="a:home")],
    ])
    text = "\n".join(lines)
    if edit:
        await _edit_or_reply(update, text, markup)
    else:
        await reply(update, text, reply_markup=markup)


async def _ban(update: Update, context: ContextTypes.DEFAULT_TYPE, banned: bool) -> None:
    s = svc(context)
    try:
        tg_id = _parse_tg_id(context.args[0])
    except (IndexError, ValueError):
        await reply(update, f"الاستخدام: <code>/{'ban' if banned else 'unban'} ID</code>")
        return
    if banned and s.is_owner(tg_id):
        await reply(update, "ما تقدر تحظر مالك.")
        return
    try:
        await s.repo.set_banned(tg_id, banned)
    except UserNotFound:
        await reply(update, "العميل غير مسجّل.")
        return
    s.forget_user(tg_id)
    await _audit(context, update, "ban" if banned else "unban", str(tg_id))
    await reply(update, f"✅ تم {'حظر' if banned else 'فك حظر'} {tg_id}")


@owner_only
async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ban(update, context, True)


@owner_only
async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ban(update, context, False)


@owner_only
async def order_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    if not context.args or not UUID_RE.match(context.args[0]):
        await reply(update, "الاستخدام: <code>/order UUID</code>")
        return
    await _order_card(update, context, context.args[0])


async def _order_card(update: Update, context: ContextTypes.DEFAULT_TYPE, order_uuid: str) -> None:
    s = svc(context)
    order = await s.repo.get_order(order_uuid)
    if order is None:
        await reply(update, "الطلب غير موجود.")
        return
    user = await s.repo.get_user_by_hash(order.user_hash)
    extra = (f"\nالعميل: <code>{user.telegram_id if user else '?'}</code>"
             f"\nتكلفة متوقعة: {order.cost} | سعر المزود: {order.provider_price or '-'}"
             f"\nرقم المزود: {esc(order.provider_order_id or '-')}"
             f"\nمحاولات: {order.attempts} | آخر خطأ: {esc(order.last_error or '-')}")
    rows = []
    if not order.is_final:
        rows.append([InlineKeyboardButton(
            "↩️ استرجاع المبلغ للعميل", callback_data=f"a:rq:{order.uuid}")])
    if _order_player_id(order):
        rows.append([InlineKeyboardButton(
            "🚫 حظر آيدي اللاعب هذا", callback_data=f"a:bo:{order.uuid}")])
    await reply(update, order_text(order, s, detailed=True) + extra,
                reply_markup=InlineKeyboardMarkup(rows) if rows else None)


@owner_only
async def refund(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    if not context.args or not UUID_RE.match(context.args[0]):
        await reply(update, "الاستخدام: <code>/refund UUID</code>")
        return
    await _refund_prompt(update, context, context.args[0])


async def _refund_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, order_uuid: str) -> None:
    s = svc(context)
    order = await s.repo.get_order(order_uuid)
    if order is None:
        await reply(update, "الطلب غير موجود.")
        return
    if order.is_final:
        await reply(update, f"الطلب حالته نهائية ({order.status})، ما يحتاج استرجاع.")
        return
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(
        "تأكيد الاسترجاع", callback_data=f"a:rf:{order.uuid}")]])
    await reply(update, order_text(order, s) +
                "\n\n⚠️ تأكد من لوحة game4station إن الطلب ما تنفّذ قبل الاسترجاع.",
                reply_markup=markup)


async def _do_refund(update: Update, context: ContextTypes.DEFAULT_TYPE, order_uuid: str) -> None:
    s = svc(context)
    order = await s.orders.owner_refund(order_uuid)
    if order is None:
        await reply(update, "الطلب صار نهائي مسبقاً، ما تم أي استرجاع.")
        return
    await _audit(context, update, "refund", order_uuid)
    await reply(update, f"✅ تم استرجاع {s.money(order.amount)} للعميل.")
    user = await s.repo.get_user_by_hash(order.user_hash)
    if user:
        try:
            await context.bot.send_message(
                user.telegram_id, "↩️ تم إلغاء طلبك ورجع المبلغ لمحفظتك.\n\n" + order_text(order, s),
                parse_mode="HTML")
        except Exception as exc:
            log.info("Could not notify %s: %s", user.telegram_id, exc)


@owner_only
async def recheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _recheck(update, context)


async def _recheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    done = await s.orders.reconcile()
    await reply(update, f"✅ تم التحقق. طلبات اكتملت الآن: {done}")


@owner_only
async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    entries = await s.repo.owner_logs(limit=20)
    if not entries:
        await reply(update, "السجل فاضي.")
        return
    lines = ["📜 <b>سجل المالك</b>"]
    lines += [f"{when(at)} | {owner} | {esc(action)} | {esc(details)}"
              for at, owner, action, details in entries]
    await reply(update, "\n".join(lines))


# ---------------------------------------------------------------------------
# Which products customers can see
# ---------------------------------------------------------------------------
@owner_only
async def products_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _pv_categories(update, context, 0)


async def _products_button(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           action: str, args: list[str]) -> None:
    """Callback data:
      a:pv:<page>                       categories
      a:pk:<cat>:<page>                 products of a category
      a:pt:<cat>:<page>:<product_id>    toggle one product
      a:pa:<cat>:<page>:<1|0>           show/hide the whole category
    """
    try:
        if action == "pv":
            await _pv_categories(update, context, int(args[0]))
            return
        key, page = args[0], int(args[1])
        if not CAT_KEY_RE.match(key) or page < 0:
            return
        if action == "pk":
            await _pv_category(update, context, key, page)
        elif action == "pt":
            await _pv_toggle(update, context, key, page, int(args[2]))
        elif action == "pa" and args[2] in {"0", "1"}:
            await _pv_whole_category(update, context, key, page, args[2] == "1")
    except (IndexError, ValueError):
        return
    except (G4SError, G4SUnavailable) as exc:
        log.warning("Catalog load failed: %s", exc)
        await reply(update, "تعذّر تحميل المنتجات من game4station الحين، جرّب بعد شوي.")


async def _edit_or_reply(update: Update, text: str, markup: InlineKeyboardMarkup) -> None:
    q = update.callback_query
    if q and q.message:
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
    await reply(update, text, reply_markup=markup)


def _nav(prefix: str, page: int, total: int) -> list[InlineKeyboardButton]:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"{prefix}:{page - 1}"))
    if (page + 1) * PV_PAGE < total:
        nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"{prefix}:{page + 1}"))
    return nav


async def _pv_categories(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    s = svc(context)
    try:
        cats = await s.catalog.categories()  # every product, visible or not
    except (G4SError, G4SUnavailable) as exc:
        log.warning("Catalog load failed: %s", exc)
        await reply(update, "تعذّر تحميل المنتجات من game4station الحين، جرّب بعد شوي.")
        return
    if not cats:
        await reply(update, "ما فيه منتجات عند المزود.")
        return
    visible = await s.runtime.visible_products()
    rows = []
    for c in cats[page * PV_PAGE:(page + 1) * PV_PAGE]:
        on = sum(1 for p in c.products if p.id in visible)
        mark = "✅" if on == len(c.products) else ("🟡" if on else "⬜")
        rows.append([InlineKeyboardButton(f"{mark} {c.name[:45]} ({on}/{len(c.products)})",
                                          callback_data=f"a:pk:{c.key}:0")])
    if nav := _nav("a:pv", page, len(cats)):
        rows.append(nav)
    shown = sum(1 for c in cats for p in c.products if p.id in visible)
    text = ("🧩 <b>المنتجات المعروضة للعملاء</b>\n"
            f"المعروض الحين: {shown} منتج\n\n"
            "✅ كل القسم ظاهر  🟡 بعضه  ⬜ مخفي\nاختر القسم:")
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def _pv_category(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str,
                       page: int) -> None:
    s = svc(context)
    cat = await s.catalog.category(key)
    if cat is None:
        await _pv_categories(update, context, 0)
        return
    visible = await s.runtime.visible_products()
    rows = []
    for p in cat.products[page * PV_PAGE:(page + 1) * PV_PAGE]:
        mark = "✅" if p.id in visible else "⬜"
        min_qty = p.qty.choices[0] if p.qty.kind == "choices" else p.qty.min
        price = await s.runtime.price(p, min_qty)
        na = " ⛔" if not p.available else ""
        rows.append([InlineKeyboardButton(f"{mark} {p.name[:35]} — {s.money(price)}{na}",
                                          callback_data=f"a:pt:{key}:{page}:{p.id}")])
    rows.append([InlineKeyboardButton("✅ إظهار الكل", callback_data=f"a:pa:{key}:{page}:1"),
                 InlineKeyboardButton("⬜ إخفاء الكل", callback_data=f"a:pa:{key}:{page}:0")])
    if nav := _nav(f"a:pk:{key}", page, len(cat.products)):
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 الأقسام", callback_data="a:pv:0")])
    text = (f"🧩 <b>{esc(cat.name)}</b>\n"
            "اضغط على المنتج عشان تظهره للعملاء أو تخفيه.\n✅ ظاهر  ⬜ مخفي")
    if any(not p.available for p in cat.products):
        text += "\n⛔ غير متوفر عند المزود (ما يظهر للعملاء لين يتوفر)"
    await _edit_or_reply(update, text, InlineKeyboardMarkup(rows))


async def _pv_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, page: int,
                     product_id: int) -> None:
    s = svc(context)
    product = await s.catalog.product(product_id)
    if product is None:
        await _pv_category(update, context, key, page)
        return
    s.runtime.invalidate()  # decide from the latest saved state, not the cache
    show = not await s.runtime.is_visible(product_id)
    await s.repo.set_products_visible([product_id], show)
    s.runtime.invalidate()
    await _audit(context, update, "product_show" if show else "product_hide",
                 f"{product_id} {product.name}")
    await _pv_category(update, context, key, page)


async def _pv_whole_category(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str,
                             page: int, show: bool) -> None:
    s = svc(context)
    cat = await s.catalog.category(key)
    if cat is None:
        await _pv_categories(update, context, 0)
        return
    await s.repo.set_products_visible([p.id for p in cat.products], show)
    s.runtime.invalidate()
    await _audit(context, update, "category_show" if show else "category_hide", cat.name)
    await _pv_category(update, context, key, page)


# ---------------------------------------------------------------------------
# Button flows: the owner taps, and types only an ID or an amount when asked.
# State lives in context.user_data (per owner, in memory):
#   owner_step = {"kind": "user_id"|"amount"|"margin"|"order", "at": t, ...}
#   owner_op   = {"id": nonce, "sign": ±1, "tg_id": int, "amount": str, "note": str}
# A money change runs only after the owner taps the confirm button carrying the
# matching nonce, and the op is removed before it runs, so a double tap can't
# apply it twice.
# ---------------------------------------------------------------------------
def _set_step(context: ContextTypes.DEFAULT_TYPE, kind: str, **data) -> None:
    context.user_data.pop("draft", None)  # drop any half-finished purchase
    context.user_data.pop("owner_op", None)
    context.user_data["owner_step"] = {"kind": kind, "at": time.monotonic(), **data}


def _get_step(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    step = context.user_data.get("owner_step")
    if step and time.monotonic() - step["at"] > STEP_TTL_SECONDS:
        context.user_data.pop("owner_step", None)
        return None
    return step


def _clear_step(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("owner_step", None)
    context.user_data.pop("owner_op", None)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Called for free text before the customer flow. Returns True if the text
    answered an owner prompt."""
    user = update.effective_user
    if user is None or not svc(context).is_owner(user.id):
        return False
    step = _get_step(context)
    if not step:
        return False
    s = svc(context)
    text = (update.effective_message.text or "").strip()
    kind = step["kind"]

    if kind == "user_id":
        try:
            tg_id = _parse_tg_id(text)
        except ValueError:
            await reply(update, "الآيدي لازم يكون أرقام بس. أرسله مرة ثانية:",
                        reply_markup=CANCEL_MARKUP)
            return True
        target = await s.repo.get_user(tg_id)
        if target is None:
            await reply(update, "هذا العميل غير مسجّل. لازم يفتح البوت ويضغط /start أول.\n"
                                "أرسل آيدي ثاني:", reply_markup=CANCEL_MARKUP)
            return True
        if step["then"] == "manage":
            _clear_step(context)
            await _user_card(update, context, tg_id)
        else:
            await _ask_amount(update, context, step["then"], tg_id)
        return True

    if kind == "amount":
        parts = text.split(maxsplit=1)
        try:
            amount = parse_user_amount(parts[0])
        except (IndexError, InvalidAmount):
            await reply(update, "مبلغ غير صالح. اكتب رقم موجب، بحد أقصى خانتين بعد الفاصلة، "
                                "مثل <code>50</code> أو <code>12.5</code>:",
                        reply_markup=CANCEL_MARKUP)
            return True
        note = parts[1][:200] if len(parts) > 1 else ""
        if step["sign"] < 0:
            target = await s.repo.get_user(step["tg_id"])
            if target is not None and target.balance < amount:
                await reply(update, f"رصيد العميل ما يكفي، رصيده {s.money(target.balance)}.\n"
                                    "أرسل مبلغ أقل:", reply_markup=CANCEL_MARKUP)
                return True
        await _ask_confirm(update, context, step["sign"], step["tg_id"], amount, note)
        return True

    if kind == "margin":
        try:
            value = _parse_margin(text)
        except InvalidAmount:
            await reply(update, "نسبة غير صالحة. اكتب رقم من 0 إلى 500، مثل <code>12</code>:",
                        reply_markup=CANCEL_MARKUP)
            return True
        _clear_step(context)
        await _set_margin(update, context, value)
        return True

    if kind == "block_player":
        if not text or len(text) > 120 or "\n" in text:
            await reply(update, "آيدي غير صالح. أرسله في سطر واحد (حد أقصى 120 حرف):",
                        reply_markup=CANCEL_MARKUP)
            return True
        _clear_step(context)
        await _block(update, context, text)
        await _blocked_list(update, context, edit=False)
        return True

    if kind == "order":
        if not UUID_RE.match(text):
            await reply(update, "رقم الطلب غير صالح. انسخه كامل من رسالة الطلب:",
                        reply_markup=CANCEL_MARKUP)
            return True
        _clear_step(context)
        await _order_card(update, context, text)
        return True

    _clear_step(context)
    return False


async def _ask_user(update: Update, context: ContextTypes.DEFAULT_TYPE, then: str) -> None:
    _set_step(context, "user_id", then=then)
    title = {"credit": "💰 شحن رصيد عميل", "debit": "➖ خصم من رصيد عميل",
             "manage": "👤 إدارة عميل"}[then]
    await reply(update, f"<b>{title}</b>\n\nأرسل آيدي العميل (أرقام).\n"
                        "العميل يلقى رقمه عنده في زر «➕ شحن الرصيد».",
                reply_markup=CANCEL_MARKUP)


async def _ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE, then: str,
                      tg_id: int) -> None:
    s = svc(context)
    target = await s.repo.get_user(tg_id)
    if target is None:
        await reply(update, "العميل غير مسجّل.")
        return
    sign = 1 if then == "credit" else -1
    _set_step(context, "amount", sign=sign, tg_id=tg_id)
    title = "💰 شحن رصيد" if sign > 0 else "➖ خصم من الرصيد"
    await reply(update, f"<b>{title}</b>\n"
                        f"العميل: {esc(target.first_name)} (<code>{tg_id}</code>)\n"
                        f"رصيده الحالي: {s.money(target.balance)}\n\n"
                        "أرسل المبلغ. وإذا تبي تضيف ملاحظة، اكتبها بعد المبلغ بمسافة، مثل:\n"
                        "<code>50 تحويل بنكي</code>",
                reply_markup=CANCEL_MARKUP)


async def _ask_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, sign: int, tg_id: int,
                       amount: Decimal, note: str) -> None:
    s = svc(context)
    context.user_data.pop("owner_step", None)
    op_id = secrets.token_hex(4)
    context.user_data["owner_op"] = {"id": op_id, "sign": sign, "tg_id": tg_id,
                                     "amount": str(amount), "note": note}
    target = await s.repo.get_user(tg_id)
    after = (target.balance + amount * sign) if target else None
    lines = ["⚠️ <b>تأكيد " + ("الشحن" if sign > 0 else "الخصم") + "</b>",
             f"العميل: {esc(target.first_name if target else '')} (<code>{tg_id}</code>)",
             f"المبلغ: <b>{'+' if sign > 0 else '-'}{s.money(amount)}</b>"]
    if note:
        lines.append(f"ملاحظة: {esc(note)}")
    if after is not None:
        lines.append(f"الرصيد بعد العملية: {s.money(after)}")
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأكيد", callback_data=f"a:cf:{op_id}"),
        InlineKeyboardButton("❌ إلغاء", callback_data="a:cx"),
    ]])
    await reply(update, "\n".join(lines), reply_markup=markup)


async def _remove_buttons(update: Update) -> None:
    q = update.callback_query
    if q and q.message:
        try:
            await q.edit_message_reply_markup(None)
        except BadRequest:
            pass


async def _btn_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, op_id: str) -> None:
    op = context.user_data.get("owner_op")
    if not op or op["id"] != op_id:
        await reply(update, "انتهت صلاحية هذي العملية، أو تنفّذت مسبقاً.")
        return
    context.user_data.pop("owner_op", None)  # before any await: a double tap finds nothing
    await _remove_buttons(update)
    await _apply_adjust(update, context, op["tg_id"], to_decimal(op["amount"]), op["sign"],
                        op["note"])


async def _btn_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE, _arg: str) -> None:
    _clear_step(context)
    await _remove_buttons(update)
    await reply(update, "تم الإلغاء.")


async def _btn_home(update: Update, context: ContextTypes.DEFAULT_TYPE, _arg: str) -> None:
    await panel(update, context)


async def _btn_help(update: Update, context: ContextTypes.DEFAULT_TYPE, _arg: str) -> None:
    await reply(update, COMMANDS_HELP)


async def _btn_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str,
                           then: str) -> None:
    try:
        tg_id = _parse_tg_id(arg)
    except ValueError:
        return
    await _ask_amount(update, context, then, tg_id)


async def _btn_toggle_ban(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str) -> None:
    s = svc(context)
    try:
        tg_id = _parse_tg_id(arg)
    except ValueError:
        return
    target = await s.repo.get_user(tg_id)
    if target is None:
        await reply(update, "العميل غير مسجّل.")
        return
    banned = not target.banned
    if banned and s.is_owner(tg_id):
        await reply(update, "ما تقدر تحظر مالك.")
        return
    await s.repo.set_banned(tg_id, banned)
    s.forget_user(tg_id)
    await _audit(context, update, "ban" if banned else "unban", str(tg_id))
    await _user_card(update, context, tg_id, edit=True)


async def _margin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool) -> None:
    s = svc(context)
    current = await s.runtime.margin()
    example = sell_total(Decimal("10"), 1, current)
    buttons = [InlineKeyboardButton(("✅ " if Decimal(p) == current else "") + f"{p}%",
                                    callback_data=f"a:ms:{p}") for p in MARGIN_PRESETS]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("✏️ اكتب نسبة ثانية", callback_data="a:mx")])
    rows.append([InlineKeyboardButton("🔙 لوحة المالك", callback_data="a:home")])
    text = (f"📈 <b>نسبة الربح الحالية: {current}%</b>\n\n"
            f"مثال: منتج سعره عند المزود {s.money(Decimal('10'))} "
            f"يطلع للعميل بـ {s.money(example)}.\n\nاختر نسبة جديدة:")
    markup = InlineKeyboardMarkup(rows)
    if edit:
        await _edit_or_reply(update, text, markup)
    else:
        await reply(update, text, reply_markup=markup)


async def _btn_margin_set(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str) -> None:
    if arg not in MARGIN_PRESETS:
        return
    await _set_margin(update, context, Decimal(arg))
    await _margin_menu(update, context, edit=True)


async def _btn_margin_custom(update: Update, context: ContextTypes.DEFAULT_TYPE, _arg: str) -> None:
    _set_step(context, "margin")
    await reply(update, "اكتب نسبة الربح اللي تبيها (رقم من 0 إلى 500)، مثل <code>12</code>:",
                reply_markup=CANCEL_MARKUP)


async def _btn_order_search(update: Update, context: ContextTypes.DEFAULT_TYPE, _arg: str) -> None:
    _set_step(context, "order")
    await reply(update, "🔍 أرسل رقم الطلب. تلقاه في رسالة الطلب عند العميل («رقم الطلب»):",
                reply_markup=CANCEL_MARKUP)


async def _btn_order_info(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str) -> None:
    if UUID_RE.match(arg):
        await _order_card(update, context, arg)


async def _btn_refund_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str) -> None:
    if UUID_RE.match(arg):
        await _refund_prompt(update, context, arg)


async def _btn_recheck(update: Update, context: ContextTypes.DEFAULT_TYPE, _arg: str) -> None:
    await _recheck(update, context)


# action -> handler(update, context, arg); `arg` is the text after "a:<action>:".
_BUTTONS = {
    "cr": lambda u, c, a: _ask_user(u, c, "credit"),
    "db": lambda u, c, a: _ask_user(u, c, "debit"),
    "us": lambda u, c, a: _ask_user(u, c, "manage"),
    "uc": lambda u, c, a: _btn_user_action(u, c, a, "credit"),
    "ud": lambda u, c, a: _btn_user_action(u, c, a, "debit"),
    "ub": _btn_toggle_ban,
    "cf": _btn_confirm,
    "cx": _btn_cancel,
    "home": _btn_home,
    "help": _btn_help,
    "mg": lambda u, c, a: _margin_menu(u, c, edit=False),
    "ms": _btn_margin_set,
    "mx": _btn_margin_custom,
    "od": _btn_order_search,
    "oi": _btn_order_info,
    "rq": _btn_refund_prompt,
    "rc": _btn_recheck,
    "bl": lambda u, c, a: _blocked_list(u, c, edit=False),
    "ba": lambda u, c, a: _btn_block_ask(u, c),
    "bu": lambda u, c, a: _btn_unblock(u, c, a),
    "bo": lambda u, c, a: _btn_block_from_order(u, c, a),
}



# ---------------------------------------------------------------------------
# Blocked player IDs (the in-game ID a customer types when ordering)
# ---------------------------------------------------------------------------
BLOCK_TAG_RE = re.compile(r"^[0-9a-f]{32}$")


def _order_player_id(order) -> str:
    """The player ID of an order: `playerId`, else the first value entered."""
    return order.params.get("playerId") or next(iter(order.params.values()), "")


async def _block(update: Update, context: ContextTypes.DEFAULT_TYPE, player_id: str) -> None:
    s = svc(context)
    created = await s.repo.block_player(player_id, update.effective_user.id)
    if created:
        await _audit(context, update, "player_block", player_id)
        await reply(update, f"✅ تم حظر الآيدي <code>{esc(player_id)}</code>.\n"
                            "أي طلب عليه ينرفض قبل الخصم، ويوصلك تنبيه.")
    else:
        await reply(update, f"الآيدي <code>{esc(player_id)}</code> محظور من قبل.")


async def _blocked_list(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool) -> None:
    s = svc(context)
    entries = await s.repo.blocked_players(limit=30)
    rows = [[InlineKeyboardButton("➕ حظر آيدي", callback_data="a:ba")]]
    rows += [[InlineKeyboardButton(f"✅ فك حظر {value[:40]}", callback_data=f"a:bu:{tag}")]
             for tag, value, _ in entries]
    rows.append([InlineKeyboardButton("🔙 لوحة المالك", callback_data="a:home")])
    if entries:
        lines = ["🚫 <b>آيديات اللاعبين المحظورة</b>", ""]
        lines += [f"• <code>{esc(value)}</code> — {when(at)}" for _, value, at in entries]
        if len(entries) == 30:
            lines.append("\n(يعرض آخر 30)")
        lines.append("\nاضغط «فك حظر» عشان ترجّع الآيدي مسموح.")
    else:
        lines = ["🚫 <b>آيديات اللاعبين المحظورة</b>", "", "ما فيه آيديات محظورة."]
    text = "\n".join(lines)
    markup = InlineKeyboardMarkup(rows)
    if edit:
        await _edit_or_reply(update, text, markup)
    else:
        await reply(update, text, reply_markup=markup)


async def _btn_block_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _set_step(context, "block_player")
    await reply(update, "🚫 أرسل آيدي اللاعب اللي تبي تحظره:", reply_markup=CANCEL_MARKUP)


async def _btn_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE, tag: str) -> None:
    if not BLOCK_TAG_RE.match(tag):
        return
    s = svc(context)
    value = await s.repo.unblock_player(tag)
    if value is not None:
        await _audit(context, update, "player_unblock", value)
    await _blocked_list(update, context, edit=True)


async def _btn_block_from_order(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                order_uuid: str) -> None:
    if not UUID_RE.match(order_uuid):
        return
    s = svc(context)
    order = await s.repo.get_order(order_uuid)
    player_id = _order_player_id(order) if order else ""
    if not player_id:
        await reply(update, "ما لقيت آيدي لاعب في هذا الطلب.")
        return
    await _block(update, context, player_id)
