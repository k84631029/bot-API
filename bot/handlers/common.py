"""Shared state, keyboards and text helpers for handlers."""
from __future__ import annotations

import html
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..catalog import Catalog, RuntimeSettings
from ..config import Settings
from ..g4s import G4SClient
from ..money import fmt
from ..orders import OrderService
from ..ratelimit import RateLimiter
from ..repo import Order, Repo

BTN_PRODUCTS = "🛒 المنتجات"
BTN_BALANCE = "💰 رصيدي"
BTN_ORDERS = "📦 طلباتي"
BTN_LEDGER = "🧾 سجل المحفظة"
BTN_TOPUP = "➕ شحن الرصيد"
BTN_ADMIN = "🛠 لوحة المالك"

STATUS_LABELS = {
    "processing": "⏳ قيد التنفيذ",
    "wait": "⏳ قيد التنفيذ",
    "unknown": "⏳ قيد التحقق",
    "accept": "✅ مكتمل",
    "reject": "❌ مرفوض (تم الاسترجاع)",
}
LEDGER_LABELS = {
    "order": "🛒 طلب",
    "refund": "↩️ استرجاع",
    "owner_credit": "➕ شحن",
    "owner_debit": "➖ خصم",
}
# Riyadh time for display.
DISPLAY_TZ = timezone(timedelta(hours=3))


@dataclass
class Services:
    settings: Settings
    repo: Repo
    api: G4SClient
    catalog: Catalog
    runtime: RuntimeSettings
    orders: OrderService
    action_limiter: RateLimiter
    order_limiter: RateLimiter
    # telegram_id -> (expires_at, banned)
    user_cache: dict[int, tuple[float, bool]] = field(default_factory=dict)

    def is_owner(self, telegram_id: int | None) -> bool:
        return telegram_id is not None and telegram_id in self.settings.owner_ids

    def money(self, amount) -> str:
        return fmt(amount, self.settings.currency_symbol)

    def forget_user(self, telegram_id: int) -> None:
        self.user_cache.pop(telegram_id, None)

    def cached_user(self, telegram_id: int) -> bool | None:
        """Return cached banned flag, or None if not cached."""
        entry = self.user_cache.get(telegram_id)
        if entry and entry[0] > time.monotonic():
            return entry[1]
        return None

    def cache_user(self, telegram_id: int, banned: bool, ttl: float = 600) -> None:
        self.user_cache[telegram_id] = (time.monotonic() + ttl, banned)


def svc(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["svc"]


def main_keyboard(is_owner: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_PRODUCTS)],
        [KeyboardButton(BTN_BALANCE), KeyboardButton(BTN_TOPUP)],
        [KeyboardButton(BTN_ORDERS), KeyboardButton(BTN_LEDGER)],
    ]
    if is_owner:
        rows.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def when(dt: datetime | None) -> str:
    if not dt:
        return "-"
    return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")


def order_text(order: Order, s: Services, detailed: bool = False) -> str:
    lines = [
        f"<b>{esc(order.product_name)}</b> × {order.qty}",
        f"الحالة: {STATUS_LABELS.get(order.status, order.status)}",
        f"المبلغ: {s.money(order.amount)}",
        f"رقم الطلب: <code>{esc(order.uuid)}</code>",
        f"التاريخ: {when(order.created_at)}",
    ]
    if detailed:
        for i, (key, value) in enumerate(order.params.items()):
            lines.append(f"{esc(param_label(order, i, key))}: <code>{esc(value)}</code>")
        if order.status == "accept" and order.replay:
            lines.append(f"الرد: <code>{esc(_flatten(order.replay))}</code>")
    return "\n".join(lines)


def param_label(order: Order, index: int, key: str) -> str:
    """The field name the customer saw (e.g. "PLAYER ID"), not the API key."""
    if index < len(order.labels) and order.labels[index].strip():
        return order.labels[index].strip().rstrip(":：").strip()
    return "PLAYER ID" if key == "playerId" else key  # orders made before labels were saved


def _flatten(replay) -> str:
    """replay_api may be a list of strings, or a list of {"replay": [...]}."""
    out: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x not in (None, ""):
            out.append(str(x))

    walk(replay)
    return "\n".join(out)[:1500]


async def reply(update: Update, text: str, **kwargs) -> None:
    kwargs.setdefault("parse_mode", "HTML")
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, **kwargs)
    elif update.effective_message:
        await update.effective_message.reply_text(text, **kwargs)
