"""Async client for the game4station API (https://api.game4station.com/api-docs)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from .money import InvalidAmount, to_decimal

log = logging.getLogger(__name__)

# Errors that mean "the order was definitely NOT created".
ORDER_ERROR_CODES = {
    100: "Insufficient balance",
    105: "Quantity not available",
    106: "Quantity not allowed",
    107: "Player ID blocked",
    108: "2FA required",
    109: "Product deleted or not found",
    110: "Product not available now",
    111: "Try again after 1 minute",
    112: "Quantity is too small",
    113: "Quantity is too large",
    114: "Unknown error",
    500: "Unknown error",
}
PUBLIC_ERROR_CODES = {
    120: "Api Token is required!",
    121: "Token error",
    122: "Not allowed to use API",
    123: "IP not allowed",
    130: "The site is under maintenance",
}
# Errors the owner must hear about (configuration / provider-side problems).
OWNER_ALERT_CODES = {100, 120, 121, 122, 123, 130}


class G4SError(Exception):
    """The API answered with status=ERROR. The request was not executed."""

    def __init__(self, code: int | None, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg


class G4SUnavailable(Exception):
    """Network failure, timeout, 5xx or malformed reply.

    For orders this means the outcome is UNKNOWN: the order may or may not
    exist. Never refund on this error; re-send the same order_uuid instead.
    """


@dataclass(frozen=True)
class QtyRule:
    kind: str  # "one" | "choices" | "range"
    choices: tuple[int, ...] = ()
    min: int = 1
    max: int = 1

    def validate(self, qty: int) -> str | None:
        """Return an error message (Arabic) or None if qty is allowed."""
        if self.kind == "one":
            return None if qty == 1 else "الكمية لهذا المنتج لازم تكون 1"
        if self.kind == "choices":
            return None if qty in self.choices else "اختر كمية من القائمة"
        if qty < self.min:
            return f"أقل كمية مسموحة {self.min}"
        if qty > self.max:
            return f"أعلى كمية مسموحة {self.max}"
        return None

    @staticmethod
    def parse(raw: Any) -> "QtyRule":
        if raw is None:
            return QtyRule("one")
        if isinstance(raw, list):
            choices = tuple(sorted({int(str(v).strip()) for v in raw if str(v).strip().isdigit()}))
            return QtyRule("choices", choices=choices) if choices else QtyRule("one")
        if isinstance(raw, dict):
            lo = int(str(raw.get("min", 1)).strip() or 1)
            hi = int(str(raw.get("max", lo)).strip() or lo)
            return QtyRule("range", min=max(lo, 1), max=max(hi, lo, 1))
        return QtyRule("one")


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    price: Decimal  # provider cost per unit
    params: tuple[str, ...]
    category_name: str
    available: bool
    qty: QtyRule
    product_type: str
    parent_id: int

    @staticmethod
    def parse(raw: dict) -> "Product":
        return Product(
            id=int(raw["id"]),
            name=str(raw.get("name") or "").strip(),
            price=to_decimal(raw.get("price", "0")),
            params=tuple(str(p).strip() for p in (raw.get("params") or []) if str(p).strip()),
            category_name=str(raw.get("category_name") or "").strip() or "أخرى",
            available=bool(raw.get("available", False)),
            qty=QtyRule.parse(raw.get("qty_values")),
            product_type=str(raw.get("product_type") or ""),
            parent_id=int(raw.get("parent_id") or 0),
        )


@dataclass(frozen=True)
class OrderResult:
    provider_order_id: str | None
    status: str  # accept | reject | wait
    price: Decimal | None
    replay: Any
    order_uuid: str | None = None


def _parse_order(data: dict) -> OrderResult:
    status = str(data.get("status", "")).lower()
    if status not in {"accept", "reject", "wait"}:
        raise G4SUnavailable(f"Unexpected order status {status!r}")
    price = None
    if data.get("price") not in (None, ""):
        try:
            price = to_decimal(data["price"])
        except InvalidAmount:
            price = None
    return OrderResult(
        provider_order_id=data.get("order_id"),
        status=status,
        price=price,
        replay=data.get("replay_api"),
        order_uuid=data.get("order_uuid") or data.get("uuid"),
    )


class G4SClient:
    def __init__(self, api_token: str, base_url: str = "https://api.game4station.com",
                 proxy: str | None = None, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"api-token": api_token, "Accept": "application/json"},
            timeout=httpx.Timeout(timeout, connect=10.0),
            proxy=proxy,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise G4SUnavailable(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 500:
            raise G4SUnavailable(f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise G4SUnavailable(f"Non-JSON response (HTTP {resp.status_code})") from exc
        if isinstance(body, dict) and str(body.get("status", "")).upper() == "ERROR":
            code = body.get("code")
            try:
                code = int(code) if code is not None else None
            except (TypeError, ValueError):
                code = None
            raise G4SError(code, str(body.get("msg") or body.get("message") or "error"))
        if resp.status_code >= 400:
            raise G4SError(None, f"HTTP {resp.status_code}")
        return body

    async def profile(self) -> tuple[Decimal, str]:
        body = await self._get("/client/api/profile")
        return to_decimal(body["balance"]), str(body.get("email", ""))

    async def products(self, ids: list[int] | None = None) -> list[Product]:
        params = {"products_id": ",".join(str(i) for i in ids)} if ids else None
        body = await self._get("/client/api/products", params)
        if not isinstance(body, list):
            raise G4SUnavailable("Unexpected products payload")
        out = []
        for raw in body:
            try:
                out.append(Product.parse(raw))
            except (KeyError, ValueError, InvalidAmount) as exc:
                log.warning("Skipping malformed product %r: %s", raw.get("id"), exc)
        return out

    async def new_order(self, product_id: int, qty: int, order_uuid: str,
                        params: dict[str, str]) -> OrderResult:
        query = {**params, "qty": str(qty), "order_uuid": order_uuid}
        body = await self._get(f"/client/api/newOrder/{int(product_id)}/params", query)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise G4SUnavailable("Unexpected newOrder payload")
        return _parse_order(data)

    async def check_by_uuid(self, order_uuids: list[str]) -> dict[str, OrderResult]:
        """Return {order_uuid: result} for the orders the provider knows about.

        The check endpoint returns provider order IDs; when a single uuid is
        queried the single result is mapped back to it.
        """
        if not order_uuids:
            return {}
        body = await self._get("/client/api/check",
                               {"orders": "[" + ",".join(order_uuids) + "]", "uuid": "1"})
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise G4SUnavailable("Unexpected check payload")
        results: dict[str, OrderResult] = {}
        parsed = [_parse_order(i) for i in items if isinstance(i, dict)]
        for res in parsed:
            if res.order_uuid in order_uuids:
                results[res.order_uuid] = res
        if len(order_uuids) == 1 and not results and len(parsed) == 1:
            results[order_uuids[0]] = parsed[0]
        return results
