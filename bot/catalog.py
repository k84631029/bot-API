"""Product catalog cache, pricing and runtime settings."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from decimal import Decimal

from .g4s import G4SClient, Product
from .money import sell_total, to_decimal
from .repo import Repo


def category_key(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()[:10]


@dataclass
class Category:
    key: str
    name: str
    products: list[Product]


class Catalog:
    """Cached provider catalog.

    Customers see only products that are available AND that the owner made
    visible. The owner's product manager sees every product.
    """

    def __init__(self, api: G4SClient, ttl_seconds: int = 300):
        self.api = api
        self.ttl = ttl_seconds
        self._products: dict[int, Product] = {}
        self._loaded_at = 0.0

    async def _refresh(self, force: bool = False) -> None:
        if not force and self._products and time.monotonic() - self._loaded_at < self.ttl:
            return
        products = await self.api.products()
        self._products = {p.id: p for p in products}
        self._loaded_at = time.monotonic()

    @staticmethod
    def _group(products) -> list[Category]:
        grouped: dict[str, list[Product]] = {}
        for p in products:
            grouped.setdefault(p.category_name, []).append(p)
        return [
            Category(category_key(name), name, sorted(items, key=lambda p: (p.price, p.name)))
            for name, items in sorted(grouped.items())
        ]

    def _pick(self, visible: frozenset[int] | None):
        """visible=None -> every product (owner manager); otherwise only
        available products whose id is in `visible` (customers)."""
        if visible is None:
            return self._products.values()
        return [p for p in self._products.values() if p.available and p.id in visible]

    async def categories(self, visible: frozenset[int] | None = None) -> list[Category]:
        await self._refresh()
        return self._group(self._pick(visible))

    async def category(self, key: str, visible: frozenset[int] | None = None) -> Category | None:
        return next((c for c in await self.categories(visible) if c.key == key), None)

    async def product(self, product_id: int) -> Product | None:
        await self._refresh()
        return self._products.get(product_id)

    async def fresh_product(self, product_id: int) -> Product | None:
        """Live price/availability right before charging the customer."""
        found = await self.api.products([product_id])
        product = next((p for p in found if p.id == product_id), None)
        if product:
            self._products[product_id] = product
        return product


def api_params(product: Product, values: list[str]) -> dict[str, str]:
    """Map the customer's answers to API query params.

    game4station documents `playerId` for the player field and accepts any extra
    key=value pairs. The first requested field is sent as playerId; further
    fields are sent under their label.
    """
    out: dict[str, str] = {}
    for i, value in enumerate(values):
        key = "playerId" if i == 0 else (product.params[i] if i < len(product.params) else f"param{i + 1}")
        out[key] = value
    return out


class RuntimeSettings:
    """Owner-editable settings stored in Firestore, cached briefly."""

    def __init__(self, repo: Repo, default_margin: Decimal, ttl_seconds: int = 30):
        self.repo = repo
        self.default_margin = default_margin
        self.ttl = ttl_seconds
        self._cache: dict = {}
        self._loaded_at = 0.0

    async def _get(self) -> dict:
        if time.monotonic() - self._loaded_at > self.ttl:
            self._cache = await self.repo.get_settings()
            self._loaded_at = time.monotonic()
        return self._cache

    def invalidate(self) -> None:
        self._loaded_at = 0.0

    async def maintenance(self) -> bool:
        return bool((await self._get()).get("maintenance", False))

    async def margin(self) -> Decimal:
        raw = (await self._get()).get("margin_percent")
        return to_decimal(raw) if raw is not None else self.default_margin

    async def visible_products(self) -> frozenset[int]:
        """Product IDs the owner allowed customers to see. Empty = none."""
        return frozenset(int(i) for i in (await self._get()).get("visible_products") or [])

    async def is_visible(self, product_id: int) -> bool:
        return product_id in await self.visible_products()

    async def price(self, product: Product, qty: int) -> Decimal:
        return sell_total(product.price, qty, await self.margin())
