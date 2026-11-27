"""Order lifecycle.

  confirm ─► debit wallet + create order (one transaction, idempotent on uuid)
          ─► send newOrder with the same uuid
               accept  ─► done
               wait    ─► poller checks by uuid until accept/reject
               reject / API error ─► automatic refund
               network error / timeout ─► status "unknown": NO refund.

  Recovering an "unknown" order (poller, every minute):
    1. ask the provider about the uuid (check?uuid=1)
         found      ─► take its status (accept / reject / wait)
    2. not found    ─► re-send newOrder with the SAME uuid (it cannot duplicate)
    3. any error on the check or the re-send ─► stay "unknown", NEVER refund.
       The original request may have gone through even if the retry is refused
       (maintenance, low provider balance, product now unavailable...).
       The owner is alerted and decides with the manual refund button.

Only the FIRST submission may refund automatically on an API error, because
then the provider answered that very request and did not create the order.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Awaitable, Callable

from .catalog import Catalog, RuntimeSettings, api_params
from .g4s import OWNER_ALERT_CODES, G4SClient, G4SError, G4SUnavailable, OrderResult
from .repo import Order, Repo, utcnow

log = logging.getLogger(__name__)

# A "processing" order older than this was interrupted (crash/restart) between
# the debit and the API reply, and is treated like "unknown".
STALE_PROCESSING = timedelta(minutes=3)
# Alert the owner after this many failed recovery attempts (one per minute),
# then again every REALERT_EVERY attempts while the order stays unresolved.
ALERT_AFTER_ATTEMPTS = 5
REALERT_EVERY = 60


class OrderRejectedBeforeDebit(Exception):
    """Validation failed; nothing was charged."""


PLAYER_BLOCKED_TEXT = "🚫 هذا الآيدي محظور أو نصاب، وتم إلغاء طلبك."


class PlayerBlocked(OrderRejectedBeforeDebit):
    """The player ID is on the owner's block list; nothing was charged."""

    def __init__(self, value: str):
        super().__init__(PLAYER_BLOCKED_TEXT)
        self.value = value


@dataclass
class PriceChanged(Exception):
    new_total: Decimal


@dataclass
class PlaceResult:
    order: Order
    duplicate: bool = False  # same uuid already processed (double tap)
    error_code: int | None = None


Notify = Callable[[Order], Awaitable[None]]
Alert = Callable[[str], Awaitable[None]]


async def _noop_notify(order: Order) -> None:
    return None


async def _noop_alert(text: str) -> None:
    return None


class OrderService:
    def __init__(self, repo: Repo, api: G4SClient, catalog: Catalog, runtime: RuntimeSettings,
                 notify_user: Notify = _noop_notify, alert_owner: Alert = _noop_alert):
        self.repo = repo
        self.api = api
        self.catalog = catalog
        self.runtime = runtime
        self.notify_user = notify_user
        self.alert_owner = alert_owner
        self._reconcile_lock = asyncio.Lock()

    async def place(self, telegram_id: int, order_uuid: str, product_id: int, qty: int,
                    values: list[str], expected_total: Decimal) -> PlaceResult:
        existing = await self.repo.get_order(order_uuid)
        if existing:
            if existing.user_hash != self.repo.user_hash(telegram_id):
                raise OrderRejectedBeforeDebit("رقم الطلب غير صالح")
            return PlaceResult(existing, duplicate=True)

        if not await self.runtime.is_visible(product_id):
            raise OrderRejectedBeforeDebit("المنتج غير متوفر حالياً")
        if (blocked := await self.repo.first_blocked(values)) is not None:
            raise PlayerBlocked(blocked)
        try:
            product = await self.catalog.fresh_product(product_id)
        except (G4SError, G4SUnavailable) as exc:
            log.warning("Product refresh failed: %s", exc)
            raise OrderRejectedBeforeDebit("الخدمة غير متاحة الحين، جرّب بعد شوي") from exc
        if product is None or not product.available:
            raise OrderRejectedBeforeDebit("المنتج غير متوفر حالياً")
        if (err := product.qty.validate(qty)):
            raise OrderRejectedBeforeDebit(err)
        if len(values) != len(product.params):
            raise OrderRejectedBeforeDebit("البيانات المطلوبة ناقصة")

        total = await self.runtime.price(product, qty)
        if total != expected_total:
            raise PriceChanged(total)

        # Raises InsufficientFunds / UserBanned with nothing charged.
        order, created = await self.repo.create_order_with_debit(
            telegram_id, order_uuid, product.id, product.name, qty, total,
            product.price * qty, api_params(product, values), labels=list(product.params))
        if not created:
            return PlaceResult(order, duplicate=True)
        await self.repo.log_user(telegram_id, "order_created", f"{order_uuid} {product.name} x{qty}")
        return await self._submit(order)

    async def _submit(self, order: Order) -> PlaceResult:
        try:
            res = await self.api.new_order(order.product_id, order.qty, order.uuid, order.params)
        except G4SError as exc:
            # The API refused: no order exists on their side -> refund.
            final = await self.repo.finalize(order.uuid, "reject", error=f"api_{exc.code}")
            if exc.code in OWNER_ALERT_CODES:
                await self.alert_owner(f"⚠️ game4station error {exc.code}: {exc.msg}")
            return PlaceResult(final or await self.repo.get_order(order.uuid), error_code=exc.code)
        except G4SUnavailable as exc:
            log.warning("Order %s outcome unknown: %s", order.uuid, exc)
            return PlaceResult(await self._still_unknown(order, "network"))
        updated, _ = await self._apply(order.uuid, res)
        return PlaceResult(updated)

    async def _recover(self, order: Order) -> tuple[Order | None, bool]:
        """Resolve an order whose outcome is unknown. Never refunds on its own;
        only a provider status of "reject" (from check or re-send) refunds."""
        try:
            found = await self.api.check_by_uuid([order.uuid])
        except G4SError as exc:
            return await self._still_unknown(order, f"check_{exc.code}"), False
        except G4SUnavailable:
            return await self._still_unknown(order, "check_network"), False
        res = found.get(order.uuid)
        if res is None:
            # The provider has no record of it: re-send the same uuid.
            try:
                res = await self.api.new_order(order.product_id, order.qty, order.uuid,
                                               order.params)
            except G4SError as exc:
                return await self._still_unknown(order, f"api_{exc.code}"), False
            except G4SUnavailable:
                return await self._still_unknown(order, "network"), False
        return await self._apply(order.uuid, res)

    async def _still_unknown(self, order: Order, error: str) -> Order | None:
        await self.repo.mark_open(order.uuid, "unknown", error=error, count_attempt=True)
        current = await self.repo.get_order(order.uuid)
        if current and (current.attempts == ALERT_AFTER_ATTEMPTS or (
                current.attempts > ALERT_AFTER_ATTEMPTS
                and (current.attempts - ALERT_AFTER_ATTEMPTS) % REALERT_EVERY == 0)):
            await self.alert_owner(
                "⚠️ طلب حالته غير معروفة\n"
                f"المنتج: {current.product_name} × {current.qty}\n"
                f"رقم الطلب: {current.uuid}\n"
                f"المحاولات: {current.attempts} | آخر خطأ: {error}\n\n"
                "البوت ما رجّع المبلغ للعميل. شيك في لوحة game4station:\n"
                "• إذا الطلب ما وصل: لوحة المالك ← 📋 الطلبات المعلّقة ← الطلب ← ↩️ استرجاع\n"
                "• إذا وصل: اتركه، والبوت يكمّل المحاولة")
        return current

    async def _apply(self, order_uuid: str, res: OrderResult) -> tuple[Order | None, bool]:
        """Record the provider's status. Returns (order, changed): `changed` is
        True only if this call moved the order to a final status."""
        if res.status == "wait":
            await self.repo.mark_open(order_uuid, "wait", provider_order_id=res.provider_order_id)
            return await self.repo.get_order(order_uuid), False
        final = await self.repo.finalize(order_uuid, res.status, provider_order_id=res.provider_order_id,
                                         provider_price=res.price, replay=res.replay)
        if final and res.price is not None and res.price > final.cost:
            log.warning("Order %s provider price %s above expected cost %s",
                        order_uuid, res.price, final.cost)
        if final is None:  # already final (e.g. the owner refunded it meanwhile)
            return await self.repo.get_order(order_uuid), False
        return final, True

    async def reconcile(self) -> int:
        """Resolve open orders. Called periodically. Returns how many finished."""
        async with self._reconcile_lock:  # job and /recheck must not overlap
            return await self._reconcile()

    async def _reconcile(self) -> int:
        finished = 0
        now = utcnow()
        for order in await self.repo.open_orders():
            try:
                if order.status == "wait":
                    found = await self.api.check_by_uuid([order.uuid])
                    res = found.get(order.uuid)
                    if res is None or res.status == "wait":
                        continue
                    updated, changed = await self._apply(order.uuid, res)
                elif order.status == "unknown" or (
                        order.status == "processing" and order.updated_at
                        and now - order.updated_at > STALE_PROCESSING):
                    updated, changed = await self._recover(order)
                else:
                    continue
            except (G4SError, G4SUnavailable) as exc:
                log.warning("Reconcile %s failed: %s", order.uuid, exc)
                continue
            if changed and updated and updated.is_final:
                finished += 1
                await self.notify_user(updated)
        return finished

    async def owner_refund(self, order_uuid: str) -> Order | None:
        """Manual refund of an open order after the owner confirmed with the
        provider that it was not delivered."""
        return await self.repo.finalize(order_uuid, "reject", error="manual_refund")
