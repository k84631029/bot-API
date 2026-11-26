"""Background jobs and notifications."""
from __future__ import annotations

import logging
import time

from telegram.ext import Application, ContextTypes

from .g4s import G4SError, G4SUnavailable
from .handlers.common import Services, order_text
from .repo import Order

log = logging.getLogger(__name__)
LOW_BALANCE_REPEAT_SECONDS = 6 * 3600


def make_notifiers(app: Application):
    async def notify_user(order: Order) -> None:
        s: Services = app.bot_data["svc"]
        user = await s.repo.get_user_by_hash(order.user_hash)
        if user is None:
            return
        head = ("✅ اكتمل طلبك" if order.status == "accept"
                else "❌ الطلب انرفض، ورجع المبلغ لمحفظتك.")
        try:
            await app.bot.send_message(user.telegram_id,
                                       f"{head}\n\n{order_text(order, s, detailed=True)}",
                                       parse_mode="HTML")
        except Exception as exc:
            log.info("Could not notify user about %s: %s", order.uuid, exc)

    async def alert_owner(text: str) -> None:
        s: Services = app.bot_data["svc"]
        for owner_id in s.settings.owner_ids:
            try:
                await app.bot.send_message(owner_id, text)
            except Exception as exc:
                log.warning("Could not alert owner %s: %s", owner_id, exc)

    return notify_user, alert_owner


async def reconcile_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    s: Services = context.application.bot_data["svc"]
    try:
        done = await s.orders.reconcile()
        if done:
            log.info("Reconciled %d orders", done)
    except Exception:
        log.exception("Reconcile job failed")


async def provider_balance_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    s: Services = context.application.bot_data["svc"]
    state = context.application.bot_data.setdefault("low_balance_alert_at", 0.0)
    try:
        balance, _ = await s.api.profile()
    except G4SError as exc:
        await s.orders.alert_owner(f"⚠️ فشل الاتصال بـ game4station: [{exc.code}] {exc.msg}")
        return
    except G4SUnavailable as exc:
        log.warning("Provider balance check failed: %s", exc)
        return
    if balance < s.settings.low_provider_balance:
        if time.monotonic() - state > LOW_BALANCE_REPEAT_SECONDS or state == 0.0:
            context.application.bot_data["low_balance_alert_at"] = time.monotonic()
            await s.orders.alert_owner(
                f"⚠️ رصيدك عند game4station قليل: {balance}\n"
                f"(الحد: {s.settings.low_provider_balance}). اشحن عشان ما تتوقف الطلبات.")
    else:
        context.application.bot_data["low_balance_alert_at"] = 0.0


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    s: Services = context.application.bot_data["svc"]
    s.action_limiter.cleanup()
    s.order_limiter.cleanup()
    now = time.monotonic()
    for key in [k for k, (exp, _) in s.user_cache.items() if exp < now]:
        s.user_cache.pop(key, None)
