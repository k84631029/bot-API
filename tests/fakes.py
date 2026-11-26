"""A fake game4station server behind httpx.MockTransport."""
from __future__ import annotations

import json
from collections import deque
from urllib.parse import parse_qs, urlparse

import httpx

from bot.security import Crypto, generate_key


def make_crypto() -> Crypto:
    return Crypto(f"v1:{generate_key()}", generate_key())


PRODUCTS = [
    {"id": 18, "name": "UC 60", "price": 1.094, "params": ["ادخل الايدي الاعب"],
     "category_name": "PUBG Global ID UC", "available": True, "qty_values": None,
     "product_type": "package", "parent_id": 7, "base_price": 0.877, "category_img": ""},
    {"id": 365, "name": "UC", "price": 0.104, "params": ["ادخل الايدي الاعب"],
     "category_name": "UC 60", "available": True, "qty_values": {"min": 1, "max": "15000"},
     "product_type": "amount", "parent_id": 0, "base_price": 0.10, "category_img": ""},
]


class FakeG4S:
    def __init__(self):
        self.products = [dict(p) for p in PRODUCTS]
        self.balance = "100.000"
        # Each entry: "accept" | "reject" | "wait" | ("error", code) | "network" | "http500"
        self.behaviours: deque = deque()
        self.orders: dict[str, dict] = {}  # order_uuid -> order
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _json(self, body, status=200):
        return httpx.Response(status, content=json.dumps(body).encode(),
                              headers={"content-type": "application/json"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("api-token") != "test-token":
            return self._json({"status": "ERROR", "msg": "error Token", "code": 121})
        url = urlparse(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path

        if path == "/client/api/profile":
            return self._json({"balance": self.balance, "email": "x@example.com"})
        if path == "/client/api/products":
            items = self.products
            if "products_id" in q:
                ids = {int(i) for i in q["products_id"].split(",")}
                items = [p for p in items if p["id"] in ids]
            return self._json(items)
        if path.startswith("/client/api/newOrder/"):
            uuid = q["order_uuid"]
            if uuid in self.orders:  # idempotent, like the real API
                return self._json({"status": "OK", "data": self.orders[uuid]})
            behaviour = self.behaviours.popleft() if self.behaviours else "accept"
            if behaviour == "network":
                raise httpx.ConnectTimeout("timeout", request=request)
            if behaviour == "network_after_create":
                self.orders[uuid] = self._order(uuid, "accept", q)
                raise httpx.ReadTimeout("timeout", request=request)
            if behaviour == "http500":
                return httpx.Response(502, content=b"Bad gateway")
            if isinstance(behaviour, tuple) and behaviour[0] == "error":
                return self._json({"status": "ERROR", "msg": "err", "code": behaviour[1]})
            self.orders[uuid] = self._order(uuid, behaviour, q)
            return self._json({"status": "OK", "data": self.orders[uuid]})
        if path == "/client/api/check":
            wanted = q["orders"].strip("[]").split(",")
            data = [dict(o, created_at="2025-04-10 13:55:48") for u, o in self.orders.items()
                    if u in wanted]
            return self._json({"status": "OK", "data": data})
        return httpx.Response(404)

    def _order(self, uuid, status, q):
        return {"order_id": f"ID_{uuid[:8]}", "status": status, "price": 1.26048,
                "data": {"playerId": q.get("playerId")},
                "replay_api": [{"replay": ["CODE-123"]}] if status == "accept" else None}
