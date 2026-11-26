"""Minimal transactional document store.

Business logic is written once against this interface; FirestoreStore is the
production backend and MemoryStore backs the tests.

Firestore rule: inside a transaction, do every read before the first write.
Transaction functions may be retried on contention, so they must not have side
effects outside the transaction (no API calls, no Telegram messages).
"""
from __future__ import annotations

import asyncio
import copy
import json
import base64
import uuid
from typing import Any, Awaitable, Callable, Iterable, Protocol, TypeVar

T = TypeVar("T")
Filter = tuple[str, str, Any]  # (field, op, value); op in == in < <= > >=


class Tx(Protocol):
    async def get(self, collection: str, doc_id: str) -> dict | None: ...
    def set(self, collection: str, doc_id: str, data: dict) -> None: ...
    def merge(self, collection: str, doc_id: str, data: dict) -> None: ...


class Store(Protocol):
    async def run_transaction(self, fn: Callable[[Tx], Awaitable[T]]) -> T: ...
    async def get(self, collection: str, doc_id: str) -> dict | None: ...
    async def set(self, collection: str, doc_id: str, data: dict) -> None: ...
    async def merge(self, collection: str, doc_id: str, data: dict) -> None: ...
    async def query(self, collection: str, filters: Iterable[Filter] = (),
                    order_by: str | None = None, descending: bool = False,
                    limit: int | None = None) -> list[tuple[str, dict]]: ...


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------
class _FsTx:
    def __init__(self, db, transaction):
        self._db = db
        self._t = transaction

    async def get(self, collection, doc_id):
        snap = await self._db.collection(collection).document(doc_id).get(transaction=self._t)
        return snap.to_dict() if snap.exists else None

    def set(self, collection, doc_id, data):
        self._t.set(self._db.collection(collection).document(doc_id), data)

    def merge(self, collection, doc_id, data):
        self._t.set(self._db.collection(collection).document(doc_id), data, merge=True)


class FirestoreStore:
    def __init__(self, db):
        self._db = db

    @classmethod
    def connect(cls, credentials_json: str | None = None, project: str | None = None) -> "FirestoreStore":
        from google.cloud import firestore

        if credentials_json:
            from google.oauth2 import service_account

            raw = credentials_json.strip()
            if not raw.startswith("{"):
                raw = base64.b64decode(raw).decode()
            info = json.loads(raw)
            creds = service_account.Credentials.from_service_account_info(info)
            db = firestore.AsyncClient(project=project or info.get("project_id"), credentials=creds)
        else:
            # Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS).
            db = firestore.AsyncClient(project=project)
        return cls(db)

    async def run_transaction(self, fn):
        from google.cloud.firestore_v1 import async_transactional

        @async_transactional
        async def _inner(transaction):
            return await fn(_FsTx(self._db, transaction))

        return await _inner(self._db.transaction())

    async def get(self, collection, doc_id):
        snap = await self._db.collection(collection).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    async def set(self, collection, doc_id, data):
        await self._db.collection(collection).document(doc_id).set(data)

    async def merge(self, collection, doc_id, data):
        await self._db.collection(collection).document(doc_id).set(data, merge=True)

    async def query(self, collection, filters=(), order_by=None, descending=False, limit=None):
        from google.cloud.firestore_v1.base_query import FieldFilter
        from google.cloud import firestore

        q = self._db.collection(collection)
        for field, op, value in filters:
            q = q.where(filter=FieldFilter(field, op, value))
        if order_by:
            q = q.order_by(order_by, direction=firestore.Query.DESCENDING if descending
                           else firestore.Query.ASCENDING)
        if limit:
            q = q.limit(limit)
        return [(snap.id, snap.to_dict()) async for snap in q.stream()]


# ---------------------------------------------------------------------------
# In-memory (tests / local experiments)
# ---------------------------------------------------------------------------
_OPS = {
    "==": lambda a, b: a == b,
    "in": lambda a, b: a in b,
    "<": lambda a, b: a is not None and a < b,
    "<=": lambda a, b: a is not None and a <= b,
    ">": lambda a, b: a is not None and a > b,
    ">=": lambda a, b: a is not None and a >= b,
}


class _MemTx:
    def __init__(self, data: dict):
        self._data = data
        self.writes: list[tuple[str, str, str, dict]] = []
        self._wrote = False

    async def get(self, collection, doc_id):
        if self._wrote:
            raise RuntimeError("Firestore requires all reads before writes in a transaction")
        doc = self._data.get(collection, {}).get(doc_id)
        return copy.deepcopy(doc)

    def set(self, collection, doc_id, data):
        self._wrote = True
        self.writes.append(("set", collection, doc_id, copy.deepcopy(data)))

    def merge(self, collection, doc_id, data):
        self._wrote = True
        self.writes.append(("merge", collection, doc_id, copy.deepcopy(data)))


class MemoryStore:
    def __init__(self):
        self.data: dict[str, dict[str, dict]] = {}
        self._lock = asyncio.Lock()

    def _apply(self, kind, collection, doc_id, data):
        coll = self.data.setdefault(collection, {})
        if kind == "merge" and doc_id in coll:
            coll[doc_id].update(data)
        else:
            coll[doc_id] = data

    async def run_transaction(self, fn):
        async with self._lock:
            tx = _MemTx(self.data)
            result = await fn(tx)
            for kind, collection, doc_id, data in tx.writes:
                self._apply(kind, collection, doc_id, data)
            return result

    async def get(self, collection, doc_id):
        return copy.deepcopy(self.data.get(collection, {}).get(doc_id))

    async def set(self, collection, doc_id, data):
        async with self._lock:
            self._apply("set", collection, doc_id, copy.deepcopy(data))

    async def merge(self, collection, doc_id, data):
        async with self._lock:
            self._apply("merge", collection, doc_id, copy.deepcopy(data))

    async def query(self, collection, filters=(), order_by=None, descending=False, limit=None):
        rows = [
            (doc_id, copy.deepcopy(doc))
            for doc_id, doc in self.data.get(collection, {}).items()
            if all(_OPS[op](doc.get(field), value) for field, op, value in filters)
        ]
        if order_by:
            rows.sort(key=lambda r: r[1].get(order_by), reverse=descending)
        return rows[:limit] if limit else rows
