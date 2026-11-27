"""Domain data access: users, wallet ledger, orders, logs, settings.

Encrypted: Telegram ID, names, balances, amounts, order params/replies, log
details. Plain (needed for queries and not sensitive): HMAC user hash, order
status, product id/name, quantities, timestamps.

Collections:
  users/{user_hash}
  orders/{order_uuid}
  ledger/{entry_id}         wallet movements (kept, no TTL)
  logs/{id}                 user activity (expire_at -> Firestore TTL policy)
  owner_logs/{id}           owner actions
  settings/global           maintenance flag, margin, visible products
  blocked_players/{tag}     player IDs nobody may top up (value encrypted)
  transfers/{id}            customer-to-customer transfers waiting for / decided by the owner
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .money import ZERO, to_decimal
from .security import Crypto
from .store import Store, Tx, new_id

USERS, ORDERS, LEDGER, LOGS, OWNER_LOGS, SETTINGS, BLOCKED, TRANSFERS = (
    "users", "orders", "ledger", "logs", "owner_logs", "settings", "blocked_players", "transfers")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def normalize_player_id(value: str) -> str:
    """Same player ID however it was typed: no spaces, Western digits, lowercase."""
    return "".join(str(value).split()).translate(_AR_DIGITS).lower()

OPEN_STATUSES = ("processing", "wait", "unknown")
FINAL_STATUSES = ("accept", "reject")


class InsufficientFunds(Exception):
    def __init__(self, balance: Decimal, needed: Decimal):
        super().__init__(f"balance {balance} < {needed}")
        self.balance = balance
        self.needed = needed


class UserNotFound(Exception):
    pass


class UserBanned(Exception):
    pass


class TransferNotAllowed(Exception):
    """Receiver missing, banned, or the sender themself."""


class TransferLimit(Exception):
    """Sender reached the daily number of transfers."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class User:
    hash: str
    telegram_id: int
    username: str
    first_name: str
    balance: Decimal
    banned: bool
    created_at: datetime | None


@dataclass
class Order:
    uuid: str
    user_hash: str
    status: str
    refunded: bool
    product_id: int
    product_name: str
    qty: int
    amount: Decimal
    cost: Decimal
    params: dict[str, str]
    labels: list[str]  # what the customer was asked for each param, same order
    provider_order_id: str | None
    provider_price: str | None
    replay: Any
    attempts: int
    last_error: str | None
    created_at: datetime | None
    updated_at: datetime | None

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES


@dataclass
class Transfer:
    id: str
    from_hash: str
    to_hash: str
    amount: Decimal
    status: str  # pending | approved | rejected
    created_at: datetime | None
    decided_at: datetime | None


@dataclass
class LedgerEntry:
    id: str
    type: str
    amount: Decimal
    balance_after: Decimal
    ref: str | None
    note: str
    created_at: datetime | None


class Repo:
    def __init__(self, store: Store, crypto: Crypto, log_retention_days: int = 180):
        self.store = store
        self.crypto = crypto
        self.retention = timedelta(days=log_retention_days)

    # -- crypto helpers ----------------------------------------------------
    def _enc(self, coll: str, doc_id: str, field: str, value: str) -> str:
        return self.crypto.encrypt(value, f"{coll}/{doc_id}/{field}")

    def _dec(self, coll: str, doc_id: str, field: str, token: str | None, default: str = "") -> str:
        if not token:
            return default
        return self.crypto.decrypt(token, f"{coll}/{doc_id}/{field}")

    def user_hash(self, telegram_id: int) -> str:
        return self.crypto.user_hash(telegram_id)

    # -- users -------------------------------------------------------------
    def _user(self, uhash: str, d: dict) -> User:
        return User(
            hash=uhash,
            telegram_id=int(self._dec(USERS, uhash, "tg_id", d.get("tg_id_enc"), "0")),
            username=self._dec(USERS, uhash, "username", d.get("username_enc")),
            first_name=self._dec(USERS, uhash, "first_name", d.get("first_name_enc")),
            balance=to_decimal(self._dec(USERS, uhash, "balance", d.get("balance_enc"), "0")),
            banned=bool(d.get("banned", False)),
            created_at=d.get("created_at"),
        )

    async def ensure_user(self, telegram_id: int, username: str | None,
                          first_name: str | None) -> tuple[User, bool]:
        """Register the user if new; refresh profile fields. Returns (user, created)."""
        uhash = self.user_hash(telegram_id)
        now = utcnow()

        async def fn(tx: Tx):
            doc = await tx.get(USERS, uhash)
            profile = {
                "username_enc": self._enc(USERS, uhash, "username", username or ""),
                "first_name_enc": self._enc(USERS, uhash, "first_name", first_name or ""),
                "last_seen": now,
            }
            if doc is None:
                doc = {
                    "tg_id_enc": self._enc(USERS, uhash, "tg_id", str(telegram_id)),
                    "balance_enc": self._enc(USERS, uhash, "balance", "0"),
                    "banned": False,
                    "created_at": now,
                    **profile,
                }
                tx.set(USERS, uhash, doc)
                return doc, True
            tx.merge(USERS, uhash, profile)
            doc.update(profile)
            return doc, False

        doc, created = await self.store.run_transaction(fn)
        return self._user(uhash, doc), created

    async def get_user(self, telegram_id: int) -> User | None:
        return await self.get_user_by_hash(self.user_hash(telegram_id))

    async def get_user_by_hash(self, uhash: str) -> User | None:
        doc = await self.store.get(USERS, uhash)
        return self._user(uhash, doc) if doc else None

    async def set_banned(self, telegram_id: int, banned: bool) -> None:
        uhash = self.user_hash(telegram_id)
        if not await self.store.get(USERS, uhash):
            raise UserNotFound(telegram_id)
        await self.store.merge(USERS, uhash, {"banned": banned})

    # -- wallet --------------------------------------------------------------
    def _ledger_doc(self, entry_id: str, uhash: str, kind: str, amount: Decimal,
                    balance_after: Decimal, ref: str | None, note: str, actor: str | None) -> dict:
        return {
            "user_hash": uhash,
            "type": kind,
            "amount_enc": self._enc(LEDGER, entry_id, "amount", str(amount)),
            "balance_after_enc": self._enc(LEDGER, entry_id, "balance_after", str(balance_after)),
            "note_enc": self._enc(LEDGER, entry_id, "note", note),
            "ref": ref,
            "actor": actor,
            "created_at": utcnow(),
        }

    async def adjust_balance(self, telegram_id: int, delta: Decimal, kind: str,
                             note: str = "", actor: str | None = None) -> Decimal:
        """Owner credit/debit. Raises InsufficientFunds if it would go negative."""
        uhash = self.user_hash(telegram_id)
        entry_id = new_id()

        async def fn(tx: Tx):
            doc = await tx.get(USERS, uhash)
            if doc is None:
                raise UserNotFound(telegram_id)
            balance = to_decimal(self._dec(USERS, uhash, "balance", doc.get("balance_enc"), "0"))
            new_balance = balance + delta
            if new_balance < ZERO:
                raise InsufficientFunds(balance, -delta)
            tx.merge(USERS, uhash, {"balance_enc": self._enc(USERS, uhash, "balance", str(new_balance))})
            tx.set(LEDGER, entry_id,
                   self._ledger_doc(entry_id, uhash, kind, delta, new_balance, None, note, actor))
            return new_balance

        return await self.store.run_transaction(fn)

    async def ledger_for_user(self, telegram_id: int, limit: int = 10) -> list[LedgerEntry]:
        uhash = self.user_hash(telegram_id)
        rows = await self.store.query(LEDGER, [("user_hash", "==", uhash)],
                                      order_by="created_at", descending=True, limit=limit)
        return [
            LedgerEntry(
                id=i,
                type=d["type"],
                amount=to_decimal(self._dec(LEDGER, i, "amount", d.get("amount_enc"), "0")),
                balance_after=to_decimal(self._dec(LEDGER, i, "balance_after",
                                                   d.get("balance_after_enc"), "0")),
                ref=d.get("ref"),
                note=self._dec(LEDGER, i, "note", d.get("note_enc")),
                created_at=d.get("created_at"),
            )
            for i, d in rows
        ]

    # -- orders --------------------------------------------------------------
    def _order(self, ouuid: str, d: dict) -> Order:
        replay_raw = self._dec(ORDERS, ouuid, "replay", d.get("replay_enc"), "null")
        return Order(
            uuid=ouuid,
            user_hash=d["user_hash"],
            status=d["status"],
            refunded=bool(d.get("refunded", False)),
            product_id=int(d["product_id"]),
            product_name=d.get("product_name", ""),
            qty=int(d.get("qty", 1)),
            amount=to_decimal(self._dec(ORDERS, ouuid, "amount", d.get("amount_enc"), "0")),
            cost=to_decimal(self._dec(ORDERS, ouuid, "cost", d.get("cost_enc"), "0")),
            params=json.loads(self._dec(ORDERS, ouuid, "params", d.get("params_enc"), "{}")),
            labels=[str(x) for x in d.get("param_labels") or []],
            provider_order_id=d.get("provider_order_id"),
            provider_price=self._dec(ORDERS, ouuid, "provider_price", d.get("provider_price_enc")) or None,
            replay=json.loads(replay_raw),
            attempts=int(d.get("attempts", 0)),
            last_error=d.get("last_error"),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
        )

    async def create_order_with_debit(self, telegram_id: int, order_uuid: str, product_id: int,
                                      product_name: str, qty: int, amount: Decimal, cost: Decimal,
                                      params: dict[str, str],
                                      labels: list[str] | None = None) -> tuple[Order, bool]:
        """Atomically debit the wallet and create the order.

        Idempotent on order_uuid: a second call (double tap, retry) returns the
        existing order with created=False and does NOT debit again.
        """
        uhash = self.user_hash(telegram_id)
        ledger_id = f"debit_{order_uuid}"
        now = utcnow()

        async def fn(tx: Tx):
            existing = await tx.get(ORDERS, order_uuid)
            user = await tx.get(USERS, uhash)
            if existing is not None:
                if existing["user_hash"] != uhash:
                    raise PermissionError("order_uuid belongs to another user")
                return existing, False
            if user is None:
                raise UserNotFound(telegram_id)
            if user.get("banned"):
                raise UserBanned(telegram_id)
            balance = to_decimal(self._dec(USERS, uhash, "balance", user.get("balance_enc"), "0"))
            if balance < amount:
                raise InsufficientFunds(balance, amount)
            new_balance = balance - amount
            doc = {
                "user_hash": uhash,
                "status": "processing",
                "refunded": False,
                "product_id": product_id,
                "product_name": product_name,
                "qty": qty,
                "amount_enc": self._enc(ORDERS, order_uuid, "amount", str(amount)),
                "cost_enc": self._enc(ORDERS, order_uuid, "cost", str(cost)),
                "param_labels": list(labels or []),
                "params_enc": self._enc(ORDERS, order_uuid, "params",
                                        json.dumps(params, ensure_ascii=False)),
                "provider_order_id": None,
                "attempts": 0,
                "last_error": None,
                "created_at": now,
                "updated_at": now,
            }
            tx.merge(USERS, uhash, {"balance_enc": self._enc(USERS, uhash, "balance", str(new_balance))})
            tx.set(ORDERS, order_uuid, doc)
            tx.set(LEDGER, ledger_id, self._ledger_doc(
                ledger_id, uhash, "order", -amount, new_balance, order_uuid, product_name, None))
            return doc, True

        doc, created = await self.store.run_transaction(fn)
        return self._order(order_uuid, doc), created

    async def get_order(self, order_uuid: str) -> Order | None:
        doc = await self.store.get(ORDERS, order_uuid)
        return self._order(order_uuid, doc) if doc else None

    async def mark_open(self, order_uuid: str, status: str, provider_order_id: str | None = None,
                        error: str | None = None, count_attempt: bool = False) -> bool:
        """Move a non-final order to processing/wait/unknown. No-op on final orders."""
        assert status in OPEN_STATUSES

        async def fn(tx: Tx):
            doc = await tx.get(ORDERS, order_uuid)
            if doc is None or doc["status"] in FINAL_STATUSES:
                return False
            patch: dict[str, Any] = {"status": status, "updated_at": utcnow(), "last_error": error}
            if provider_order_id:
                patch["provider_order_id"] = provider_order_id
            if count_attempt:
                patch["attempts"] = int(doc.get("attempts", 0)) + 1
            tx.merge(ORDERS, order_uuid, patch)
            return True

        return await self.store.run_transaction(fn)

    async def finalize(self, order_uuid: str, status: str, provider_order_id: str | None = None,
                       provider_price: Decimal | None = None, replay: Any = None,
                       error: str | None = None) -> Order | None:
        """Set accept/reject exactly once. A reject refunds the wallet in the same
        transaction. Returns the updated order, or None if it was already final
        (so callers notify the user only once)."""
        assert status in FINAL_STATUSES
        ledger_id = f"refund_{order_uuid}"

        async def fn(tx: Tx):
            doc = await tx.get(ORDERS, order_uuid)
            if doc is None or doc["status"] in FINAL_STATUSES:
                return None
            uhash = doc["user_hash"]
            user = await tx.get(USERS, uhash) if status == "reject" else None
            patch: dict[str, Any] = {"status": status, "updated_at": utcnow(), "last_error": error}
            if provider_order_id:
                patch["provider_order_id"] = provider_order_id
            if provider_price is not None:
                patch["provider_price_enc"] = self._enc(ORDERS, order_uuid, "provider_price",
                                                        str(provider_price))
            if replay is not None:
                patch["replay_enc"] = self._enc(ORDERS, order_uuid, "replay",
                                                json.dumps(replay, ensure_ascii=False))
            if status == "reject" and not doc.get("refunded"):
                amount = to_decimal(self._dec(ORDERS, order_uuid, "amount", doc.get("amount_enc"), "0"))
                balance = to_decimal(self._dec(USERS, uhash, "balance",
                                               (user or {}).get("balance_enc"), "0"))
                new_balance = balance + amount
                patch["refunded"] = True
                tx.merge(USERS, uhash, {"balance_enc": self._enc(USERS, uhash, "balance", str(new_balance))})
                tx.set(LEDGER, ledger_id, self._ledger_doc(
                    ledger_id, uhash, "refund", amount, new_balance, order_uuid,
                    doc.get("product_name", ""), None))
            tx.merge(ORDERS, order_uuid, patch)
            doc.update(patch)
            return doc

        doc = await self.store.run_transaction(fn)
        return self._order(order_uuid, doc) if doc else None

    async def orders_for_user(self, telegram_id: int, limit: int = 10) -> list[Order]:
        rows = await self.store.query(ORDERS, [("user_hash", "==", self.user_hash(telegram_id))],
                                      order_by="created_at", descending=True, limit=limit)
        return [self._order(i, d) for i, d in rows]

    async def open_orders(self, limit: int = 100) -> list[Order]:
        rows = await self.store.query(ORDERS, [("status", "in", list(OPEN_STATUSES))], limit=limit)
        return [self._order(i, d) for i, d in rows]

    # -- logs ----------------------------------------------------------------
    async def log_user(self, telegram_id: int, action: str, details: str = "") -> None:
        log_id = new_id()
        now = utcnow()
        await self.store.set(LOGS, log_id, {
            "user_hash": self.user_hash(telegram_id),
            "action": action,
            "details_enc": self._enc(LOGS, log_id, "details", details),
            "created_at": now,
            "expire_at": now + self.retention,  # Firestore TTL deletes it after this
        })

    async def log_owner(self, owner_id: int, action: str, details: str = "") -> None:
        log_id = new_id()
        await self.store.set(OWNER_LOGS, log_id, {
            "owner_hash": self.user_hash(owner_id),
            "owner_enc": self._enc(OWNER_LOGS, log_id, "owner", str(owner_id)),
            "action": action,
            "details_enc": self._enc(OWNER_LOGS, log_id, "details", details),
            "created_at": utcnow(),
        })

    async def owner_logs(self, limit: int = 20) -> list[tuple[datetime, int, str, str]]:
        rows = await self.store.query(OWNER_LOGS, order_by="created_at", descending=True, limit=limit)
        return [
            (d.get("created_at"),
             int(self._dec(OWNER_LOGS, i, "owner", d.get("owner_enc"), "0")),
             d.get("action", ""),
             self._dec(OWNER_LOGS, i, "details", d.get("details_enc")))
            for i, d in rows
        ]

    # -- settings ------------------------------------------------------------
    async def get_settings(self) -> dict:
        return await self.store.get(SETTINGS, "global") or {}

    async def update_settings(self, **fields) -> None:
        await self.store.merge(SETTINGS, "global", fields)

    async def set_products_visible(self, product_ids, visible: bool) -> frozenset[int]:
        """Show or hide products for customers. Returns the new visible set.

        Runs in a transaction so two owners toggling at once don't overwrite
        each other's changes.
        """
        ids = {int(i) for i in product_ids}

        async def fn(tx: Tx):
            doc = await tx.get(SETTINGS, "global") or {}
            current = {int(i) for i in doc.get("visible_products") or []}
            current = current | ids if visible else current - ids
            tx.merge(SETTINGS, "global", {"visible_products": sorted(current)})
            return frozenset(current)

        return await self.store.run_transaction(fn)


    # -- transfers between customers ------------------------------------------
    # The amount leaves the sender's wallet when the transfer is requested (held)
    # and reaches the receiver only when the owner approves; a rejection gives it
    # back to the sender. Every step is one transaction and happens at most once.
    def _transfer(self, tid: str, d: dict) -> Transfer:
        return Transfer(
            id=tid,
            from_hash=d["from_hash"],
            to_hash=d["to_hash"],
            amount=to_decimal(self._dec(TRANSFERS, tid, "amount", d.get("amount_enc"), "0")),
            status=d["status"],
            created_at=d.get("created_at"),
            decided_at=d.get("decided_at"),
        )

    def _name_of(self, uhash: str, doc: dict | None, fallback: str = "") -> str:
        if not doc:
            return fallback
        return self._dec(USERS, uhash, "first_name", doc.get("first_name_enc")) or fallback

    async def transfers_today(self, telegram_id: int, day: str) -> int:
        doc = await self.store.get(USERS, self.user_hash(telegram_id)) or {}
        return int(doc.get("transfer_count", 0)) if doc.get("transfer_day") == day else 0

    async def create_transfer(self, transfer_id: str, from_id: int, to_id: int, amount: Decimal,
                              day: str, per_day: int) -> tuple[Transfer, Decimal, bool]:
        """Hold `amount` from the sender and record a pending transfer.
        Returns (transfer, sender's new balance, created). Idempotent on transfer_id."""
        fh, th = self.user_hash(from_id), self.user_hash(to_id)

        async def fn(tx: Tx):
            existing = await tx.get(TRANSFERS, transfer_id)
            sender = await tx.get(USERS, fh)
            receiver = await tx.get(USERS, th)
            if sender is None:
                raise UserNotFound(from_id)
            balance = to_decimal(self._dec(USERS, fh, "balance", sender.get("balance_enc"), "0"))
            if existing is not None:
                return existing, balance, False
            if receiver is None or fh == th or receiver.get("banned"):
                raise TransferNotAllowed(to_id)
            if sender.get("banned"):
                raise UserBanned(from_id)
            count = int(sender.get("transfer_count", 0)) if sender.get("transfer_day") == day else 0
            if count >= per_day:
                raise TransferLimit(per_day)
            if balance < amount:
                raise InsufficientFunds(balance, amount)
            new_balance = balance - amount
            now = utcnow()
            doc = {
                "from_hash": fh,
                "to_hash": th,
                "amount_enc": self._enc(TRANSFERS, transfer_id, "amount", str(amount)),
                "status": "pending",
                "created_at": now,
                "decided_at": None,
                "decided_by": None,
            }
            tx.merge(USERS, fh, {"balance_enc": self._enc(USERS, fh, "balance", str(new_balance)),
                                 "transfer_day": day, "transfer_count": count + 1})
            tx.set(TRANSFERS, transfer_id, doc)
            ledger_id = f"xfer_out_{transfer_id}"
            tx.set(LEDGER, ledger_id, self._ledger_doc(
                ledger_id, fh, "transfer_out", -amount, new_balance, transfer_id,
                self._name_of(th, receiver, str(to_id)), None))
            return doc, new_balance, True

        doc, new_balance, created = await self.store.run_transaction(fn)
        return self._transfer(transfer_id, doc), new_balance, created

    async def decide_transfer(self, transfer_id: str, approve: bool,
                              owner_id: int) -> tuple[Transfer, Decimal] | None:
        """Approve (credit the receiver) or reject (refund the sender) a pending
        transfer. Returns (transfer, the credited party's new balance), or None if
        it was already decided."""

        async def fn(tx: Tx):
            doc = await tx.get(TRANSFERS, transfer_id)
            if doc is None or doc["status"] != "pending":
                return None
            sender = await tx.get(USERS, doc["from_hash"])
            receiver = await tx.get(USERS, doc["to_hash"])
            amount = to_decimal(self._dec(TRANSFERS, transfer_id, "amount", doc.get("amount_enc"), "0"))
            # approve -> money goes to the receiver; reject -> back to the sender
            target_hash, target = ((doc["to_hash"], receiver) if approve
                                   else (doc["from_hash"], sender))
            balance = to_decimal(self._dec(USERS, target_hash, "balance",
                                           (target or {}).get("balance_enc"), "0"))
            new_balance = balance + amount
            if approve:
                kind, ledger_id = "transfer_in", f"xfer_in_{transfer_id}"
                note = self._name_of(doc["from_hash"], sender)
            else:
                kind, ledger_id = "transfer_refund", f"xfer_back_{transfer_id}"
                note = self._name_of(doc["to_hash"], receiver)
            patch = {"status": "approved" if approve else "rejected", "decided_at": utcnow(),
                     "decided_by": self.user_hash(owner_id)}
            tx.merge(USERS, target_hash,
                     {"balance_enc": self._enc(USERS, target_hash, "balance", str(new_balance))})
            tx.set(LEDGER, ledger_id, self._ledger_doc(
                ledger_id, target_hash, kind, amount, new_balance, transfer_id, note, None))
            tx.merge(TRANSFERS, transfer_id, patch)
            doc.update(patch)
            return doc, new_balance

        result = await self.store.run_transaction(fn)
        if result is None:
            return None
        doc, new_balance = result
        return self._transfer(transfer_id, doc), new_balance

    async def get_transfer(self, transfer_id: str) -> Transfer | None:
        doc = await self.store.get(TRANSFERS, transfer_id)
        return self._transfer(transfer_id, doc) if doc else None

    async def pending_transfers(self, limit: int = 30) -> list[Transfer]:
        rows = await self.store.query(TRANSFERS, [("status", "==", "pending")])
        items = [self._transfer(i, d) for i, d in rows]
        items.sort(key=lambda t: t.created_at or utcnow(), reverse=True)
        return items[:limit]

    async def recent_transfers(self, status: str, limit: int = 20) -> list[Transfer]:
        """Newest decided transfers of one status, among the latest 300 overall
        (ordering by date alone needs no extra Firestore index)."""
        rows = await self.store.query(TRANSFERS, order_by="created_at", descending=True, limit=300)
        return [self._transfer(i, d) for i, d in rows if d.get("status") == status][:limit]

    # -- custom display names ---------------------------------------------------
    async def set_display_name(self, kind: str, key: str, name: str | None) -> None:
        """Owner's name for a category (key = category key) or a product
        (key = product id). name=None goes back to the provider's name."""
        field = {"category": "category_names", "product": "product_names"}[kind]

        async def fn(tx: Tx):
            doc = await tx.get(SETTINGS, "global") or {}
            names = dict(doc.get(field) or {})
            if name:
                names[str(key)] = name
            else:
                names.pop(str(key), None)
            # set (not merge): merge would keep a removed key inside the map
            tx.set(SETTINGS, "global", {**doc, field: names})

        await self.store.run_transaction(fn)

    # -- blocked player IDs ---------------------------------------------------
    def _player_tag(self, value: str) -> str:
        # 32 hex chars keeps the ID short enough for Telegram button data.
        return self.crypto.tag("player", normalize_player_id(value))[:32]

    async def block_player(self, value: str, owner_id: int) -> bool:
        """Block a player ID. Returns False if it was already blocked."""
        tag = self._player_tag(value)
        if not normalize_player_id(value):
            raise ValueError("empty player id")

        async def fn(tx: Tx):
            if await tx.get(BLOCKED, tag) is not None:
                return False
            tx.set(BLOCKED, tag, {
                "value_enc": self._enc(BLOCKED, tag, "value", value.strip()),
                "by": self.user_hash(owner_id),
                "created_at": utcnow(),
            })
            return True

        return await self.store.run_transaction(fn)

    async def unblock_player(self, tag: str) -> str | None:
        """Remove a block by its tag. Returns the player ID, or None if not found."""
        doc = await self.store.get(BLOCKED, tag)
        if doc is None:
            return None
        await self.store.delete(BLOCKED, tag)
        return self._dec(BLOCKED, tag, "value", doc.get("value_enc"))

    async def is_player_blocked(self, value: str) -> bool:
        if not normalize_player_id(value):
            return False
        return await self.store.get(BLOCKED, self._player_tag(value)) is not None

    async def first_blocked(self, values) -> str | None:
        for value in values:
            if await self.is_player_blocked(value):
                return value
        return None

    async def blocked_players(self, limit: int = 30) -> list[tuple[str, str, datetime | None]]:
        rows = await self.store.query(BLOCKED, order_by="created_at", descending=True, limit=limit)
        return [(tag, self._dec(BLOCKED, tag, "value", d.get("value_enc")), d.get("created_at"))
                for tag, d in rows]
