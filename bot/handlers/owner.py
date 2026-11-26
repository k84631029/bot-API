"""Owner tools. Every handler re-checks the caller's Telegram ID against
OWNER_IDS (from the environment) — hiding the button is not protection."""
from __future__ import annotations

import functools
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..g4s import G4SError, G4SUnavailable
from ..money import InvalidAmount, parse_user_amount, to_decimal
from ..repo import InsufficientFunds, UserNotFound
from .common import esc, order_text, reply, svc, when

log = logging.getLogger(__name__)
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

HELP = """🛠 <b>لوحة المالك</b>

<code>/credit ID المبلغ [ملاحظة]</code> — شحن رصيد عميل
<code>/debit ID المبلغ [ملاحظة]</code> — خصم من رصيد عميل
<code>/user ID</code> — معلومات عميل وآخر طلباته
<code>/ban ID</code> و <code>/unban ID</code>
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
    await reply(update, f"{HELP}\n\nالصيانة: {status}\nنسبة الربح: {margin}%",
                reply_markup=_panel_markup(maintenance))


@owner_only
async def panel_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    action = q.data.split(":")[1]
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
