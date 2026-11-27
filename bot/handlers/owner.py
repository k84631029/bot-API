"""Owner tools. Every handler re-checks the caller's Telegram ID against
OWNER_IDS (from the environment) — hiding the button is not protection."""
from __future__ import annotations

import functools
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..g4s import G4SError, G4SUnavailable
from ..money import InvalidAmount, parse_user_amount, to_decimal
from ..repo import InsufficientFunds, UserNotFound
from .common import esc, order_text, reply, svc, when

log = logging.getLogger(__name__)
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
CAT_KEY_RE = re.compile(r"^[0-9a-f]{10}$")
PV_PAGE = 10

HELP = """🛠 <b>لوحة المالك</b>

<code>/credit ID المبلغ [ملاحظة]</code> — شحن رصيد عميل
<code>/debit ID المبلغ [ملاحظة]</code> — خصم من رصيد عميل
<code>/user ID</code> — معلومات عميل وآخر طلباته
<code>/ban ID</code> و <code>/unban ID</code>
<code>/products</code> — اختيار المنتجات اللي تظهر للعملاء
<code>/margin النسبة</code> — نسبة الربح (مثال 10)
<code>/maintenance on|off</code> — وضع الصيانة
<code>/order UUID</code> — تفاصيل طلب
<code>/refund UUID</code> — استرجاع يدوي لطلب معلّق
<code>/recheck</code> — تحقق الآن من الطلبات المعلّقة
<code>/logs</code> — سجل المالك"""


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
    raw = raw.strip()
    if not raw.isdigit():
        raise ValueError
    return int(raw)


async def _audit(context, update: Update, action: str, details: str) -> None:
    await svc(context).repo.log_owner(update.effective_user.id, action, details)


def _panel_markup(maintenance: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 رصيدي عند المزود", callback_data="a:bal")],
        [InlineKeyboardButton("🧩 المنتجات المعروضة", callback_data="a:pv:0")],
        [InlineKeyboardButton("📋 الطلبات المعلّقة", callback_data="a:open")],
        [InlineKeyboardButton("🟢 إيقاف الصيانة" if maintenance else "🔴 تشغيل الصيانة",
                              callback_data="a:mt")],
        [InlineKeyboardButton("📜 سجل المالك", callback_data="a:logs")],
    ])


@owner_only
async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    maintenance = await s.runtime.maintenance()
    margin = await s.runtime.margin()
    status = "🔴 مفعّل" if maintenance else "🟢 البيع شغّال"
    shown = len(await s.runtime.visible_products())
    await reply(update, f"{HELP}\n\nالصيانة: {status}\nنسبة الربح: {margin}%"
                        f"\nالمنتجات المعروضة للعملاء: {shown}",
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
    elif action == "rf":
        await _do_refund(update, context, q.data.split(":", 2)[2])


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
    lines = [f"📋 <b>الطلبات المعلّقة ({len(orders)})</b>"]
    for o in orders:
        lines.append(f"• <code>{o.uuid}</code> {esc(o.product_name)} — {o.status} "
                     f"(محاولات {o.attempts}) {when(o.created_at)}")
    await reply(update, "\n".join(lines))


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
        value = to_decimal(context.args[0])
        if value < 0 or value > 500:
            raise InvalidAmount
    except InvalidAmount:
        await reply(update, "نسبة غير صالحة (0 إلى 500).")
        return
    await s.repo.update_settings(margin_percent=str(value))
    s.runtime.invalidate()
    await _audit(context, update, "margin", str(value))
    await reply(update, f"✅ نسبة الربح صارت {value}%")


async def _adjust(update: Update, context: ContextTypes.DEFAULT_TYPE, sign: int) -> None:
    s = svc(context)
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
    await reply(update, "\n".join(lines))


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
    order = await s.repo.get_order(context.args[0])
    if order is None:
        await reply(update, "الطلب غير موجود.")
        return
    user = await s.repo.get_user_by_hash(order.user_hash)
    extra = (f"\nالعميل: <code>{user.telegram_id if user else '?'}</code>"
             f"\nتكلفة متوقعة: {order.cost} | سعر المزود: {order.provider_price or '-'}"
             f"\nرقم المزود: {esc(order.provider_order_id or '-')}"
             f"\nمحاولات: {order.attempts} | آخر خطأ: {esc(order.last_error or '-')}")
    await reply(update, order_text(order, s, detailed=True) + extra)


@owner_only
async def refund(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = svc(context)
    if not context.args or not UUID_RE.match(context.args[0]):
        await reply(update, "الاستخدام: <code>/refund UUID</code>")
        return
    order = await s.repo.get_order(context.args[0])
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
