"""Order lifecycle.

  confirm ─► debit wallet + create order (one transaction, idempotent on uuid)
          ─► send newOrder with the same uuid
               accept  ─► done
               wait    ─► poller checks by uuid until accept/reject
               reject / API error ─► automatic refund
               network error / timeout ─► status "unknown": NO refund.
                    The poller re-sends newOrder with the SAME order_uuid; the
                    provider returns the original order if it exists, so this is
                    safe and resolves the uncertainty.
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
ALERT_AFTER_ATTEMPTS = 5


class OrderRejectedBeforeDebit(Exception):
    """Validation failed; nothing was charged."""


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
            product.price * qty, api_params(product, values))
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
            await self.repo.mark_open(order.uuid, "unknown", error="network", count_attempt=True)
            current = await self.repo.get_order(order.uuid)
            if current and current.attempts == ALERT_AFTER_ATTEMPTS:
                await self.alert_owner(
                    f"⚠️ الطلب {order.uuid} حالته غير معروفة بعد {current.attempts} محاولات. "
                    "راجع لوحة game4station.")
            return PlaceResult(current)
        updated = await self._apply(order.uuid, res)
        return PlaceResult(updated)

    async def _apply(self, order_uuid: str, res: OrderResult) -> Order:
        if res.status == "wait":
            await self.repo.mark_open(order_uuid, "wait", provider_order_id=res.provider_order_id)
            return await self.repo.get_order(order_uuid)
        final = await self.repo.finalize(order_uuid, res.status, provider_order_id=res.provider_order_id,
                                         provider_price=res.price, replay=res.replay)
        if final and res.price is not None and res.price > final.cost:
            log.warning("Order %s provider price %s above expected cost %s",
                        order_uuid, res.price, final.cost)
        return final or await self.repo.get_order(order_uuid)

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
                    updated = await self._apply(order.uuid, res)
                elif order.status == "unknown" or (
                        order.status == "processing" and order.updated_at
                        and now - order.updated_at > STALE_PROCESSING):
                    updated = (await self._submit(order)).order
                else:
                    continue
            except (G4SError, G4SUnavailable) as exc:
                log.warning("Reconcile %s failed: %s", order.uuid, exc)
                continue
            if updated and updated.is_final:
                finished += 1
                await self.notify_user(updated)
        return finished

    async def owner_refund(self, order_uuid: str) -> Order | None:
        """Manual refund of an open order after the owner confirmed with the
        provider that it was not delivered."""
        return await self.repo.finalize(order_uuid, "reject", error="manual_refund")
