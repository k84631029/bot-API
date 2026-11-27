import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from bot.catalog import Catalog, RuntimeSettings
from bot.g4s import G4SClient, G4SUnavailable
from bot.orders import OrderRejectedBeforeDebit, OrderService, PlayerBlocked, PriceChanged
from bot.repo import InsufficientFunds, Repo, utcnow
from bot.security import DecryptionError
from bot.store import MemoryStore
from tests.fakes import FakeG4S, make_crypto

USER = 987654321
OTHER = 123456789
PRICE_18 = Decimal("1.21")  # 1.094 * 1.10 rounded up


class Env:
    def __init__(self):
        self.fake = FakeG4S()
        self.store = MemoryStore()
        self.repo = Repo(self.store, make_crypto())
        self.api = G4SClient("test-token", transport=self.fake.transport())
        self.catalog = Catalog(self.api)
        self.runtime = RuntimeSettings(self.repo, Decimal("10"))
        self.notified = []
        self.alerts = []

        async def notify(order):
            self.notified.append(order)

        async def alert(text):
            self.alerts.append(text)

        self.svc = OrderService(self.repo, self.api, self.catalog, self.runtime, notify, alert)

    async def user(self, tg_id=USER, balance="10"):
        await self.repo.ensure_user(tg_id, "name", "First")
        if Decimal(balance):
            await self.repo.adjust_balance(tg_id, Decimal(balance), "owner_credit")

    async def balance(self, tg_id=USER):
        return (await self.repo.get_user(tg_id)).balance

    async def place(self, uuid="11111111-1111-4111-8111-111111111111", total=PRICE_18, tg_id=USER):
        return await self.svc.place(tg_id, uuid, 18, 1, ["PLAYER-ZX81Q"], total)


@pytest.fixture
async def env():
    e = Env()
    await e.repo.set_products_visible([18, 365], True)
    await e.user()
    yield e
    await e.api.aclose()


async def test_accept_debits_once(env):
    res = await env.place()
    assert res.order.status == "accept"
    assert await env.balance() == Decimal("8.79")
    assert res.order.replay == [{"replay": ["CODE-123"]}]
    ledger = await env.repo.ledger_for_user(USER)
    assert [e.type for e in ledger] == ["order", "owner_credit"]


async def test_reject_refunds(env):
    env.fake.behaviours.append("reject")
    res = await env.place()
    assert res.order.status == "reject" and res.order.refunded
    assert await env.balance() == Decimal("10")
    assert [e.type for e in await env.repo.ledger_for_user(USER)][0] == "refund"


async def test_api_error_refunds_and_alerts_owner(env):
    env.fake.behaviours.append(("error", 100))
    res = await env.place()
    assert res.error_code == 100
    assert res.order.status == "reject"
    assert await env.balance() == Decimal("10")
    assert env.alerts and "100" in env.alerts[0]


async def test_network_error_never_refunds_then_resolves_by_uuid(env):
    # The provider created the order but the reply was lost.
    env.fake.behaviours.append("network_after_create")
    res = await env.place()
    assert res.order.status == "unknown"
    assert await env.balance() == Decimal("8.79")  # still charged, NOT refunded

    assert await env.svc.reconcile() == 1
    order = await env.repo.get_order(res.order.uuid)
    assert order.status == "accept"
    assert len(env.fake.orders) == 1  # re-sending the same uuid did not duplicate
    assert await env.balance() == Decimal("8.79")
    assert [o.uuid for o in env.notified] == [order.uuid]


async def test_network_error_before_create_gets_submitted_by_poller(env):
    env.fake.behaviours.extend(["network", "accept"])
    res = await env.place()
    assert res.order.status == "unknown"
    await env.svc.reconcile()
    assert (await env.repo.get_order(res.order.uuid)).status == "accept"


async def test_wait_then_reject_refunds_exactly_once(env):
    env.fake.behaviours.append("wait")
    res = await env.place()
    assert res.order.status == "wait"
    assert await env.svc.reconcile() == 0  # still waiting

    env.fake.orders[res.order.uuid]["status"] = "reject"
    assert await env.svc.reconcile() == 1
    assert await env.svc.reconcile() == 0
    assert await env.balance() == Decimal("10")
    assert len(env.notified) == 1


async def test_double_tap_charges_once(env):
    uuid = "22222222-2222-4222-8222-222222222222"
    results = await asyncio.gather(env.place(uuid), env.place(uuid), env.place(uuid))
    assert sum(not r.duplicate for r in results) == 1
    assert await env.balance() == Decimal("8.79")
    assert len(env.fake.orders) == 1


async def test_insufficient_funds_charges_nothing(env):
    await env.user(OTHER, balance="0")
    with pytest.raises(InsufficientFunds):
        await env.place(tg_id=OTHER)
    assert await env.repo.orders_for_user(OTHER) == []
    assert env.fake.orders == {}


async def test_price_change_requires_reconfirm(env):
    env.fake.products[0]["price"] = 2.0
    with pytest.raises(PriceChanged) as exc:
        await env.place()
    assert exc.value.new_total == Decimal("2.20")
    assert await env.balance() == Decimal("10")


async def test_unavailable_product_charges_nothing(env):
    env.fake.products[0]["available"] = False
    with pytest.raises(OrderRejectedBeforeDebit):
        await env.place()
    assert await env.balance() == Decimal("10")


async def test_uuid_of_another_user_is_rejected(env):
    await env.user(OTHER, balance="10")
    res = await env.place()
    with pytest.raises(OrderRejectedBeforeDebit):
        await env.place(uuid=res.order.uuid, tg_id=OTHER)


async def test_stale_processing_order_is_recovered(env):
    # Simulate a crash right after the debit, before the API call.
    uuid = "33333333-3333-4333-8333-333333333333"
    await env.repo.create_order_with_debit(USER, uuid, 18, "UC 60", 1, PRICE_18,
                                           Decimal("1.094"), {"playerId": "1"})
    env.store.data["orders"][uuid]["updated_at"] = utcnow() - timedelta(minutes=10)
    await env.svc.reconcile()
    assert (await env.repo.get_order(uuid)).status == "accept"


async def test_owner_refund_only_for_open_orders(env):
    res = await env.place()  # accepted
    assert await env.svc.owner_refund(res.order.uuid) is None
    env.fake.behaviours.append("wait")
    res2 = await env.place(uuid="44444444-4444-4444-8444-444444444444")
    refunded = await env.svc.owner_refund(res2.order.uuid)
    assert refunded.status == "reject"
    assert await env.svc.owner_refund(res2.order.uuid) is None
    assert await env.balance() == Decimal("8.79")


async def test_owner_debit_cannot_go_negative(env):
    with pytest.raises(InsufficientFunds):
        await env.repo.adjust_balance(USER, Decimal("-50"), "owner_debit")
    assert await env.balance() == Decimal("10")


async def test_database_holds_no_plain_identity_or_balance(env):
    await env.place()
    user_doc_id, user_doc = next(iter(env.store.data["users"].items()))
    assert user_doc_id == env.repo.user_hash(USER)
    dump = repr(env.store.data)
    assert str(USER) not in dump
    assert "PLAYER-ZX81Q" not in dump  # player id
    assert "8.79" not in dump  # balance


async def test_copying_balance_between_users_fails(env):
    await env.user(OTHER, balance="0")
    users = env.store.data["users"]
    users[env.repo.user_hash(OTHER)]["balance_enc"] = users[env.repo.user_hash(USER)]["balance_enc"]
    with pytest.raises(DecryptionError):
        await env.repo.get_user(OTHER)


async def test_hidden_product_cannot_be_ordered(env):
    await env.repo.set_products_visible([18], False)
    env.runtime.invalidate()
    with pytest.raises(OrderRejectedBeforeDebit):
        await env.place()
    assert await env.balance() == Decimal("10")
    assert env.fake.orders == {}


async def test_visibility_toggles_are_merged(env):
    await env.repo.set_products_visible([18, 365], False)
    assert await env.repo.set_products_visible([18], True) == frozenset({18})
    assert await env.repo.set_products_visible([365], True) == frozenset({18, 365})
    assert await env.repo.set_products_visible([18], False) == frozenset({365})


async def test_blocked_player_cannot_order(env):
    assert await env.repo.block_player(" player-zx81q ", owner_id=1)  # spaces/case ignored
    assert not await env.repo.block_player("PLAYER-ZX81Q", owner_id=1)  # already blocked
    with pytest.raises(PlayerBlocked) as info:
        await env.place()
    assert "محظور أو نصاب" in str(info.value)
    assert await env.balance() == Decimal("10")
    assert env.fake.orders == {}

    [(tag, value, _)] = await env.repo.blocked_players()
    assert value == "player-zx81q"
    assert await env.repo.unblock_player(tag) == "player-zx81q"
    assert await env.repo.blocked_players() == []
    res = await env.place()
    assert res.order.status == "accept"


async def test_blocked_player_id_is_not_stored_in_plain_text(env):
    await env.repo.block_player("5123456789", owner_id=1)
    assert "5123456789" not in repr(env.store.data["blocked_players"])
    assert await env.repo.is_player_blocked("٥١٢٣٤٥٦٧٨٩")  # Arabic digits match too



def _new_orders(env):
    return [r for r in env.fake.requests if "/newOrder/" in r.url.path]


async def test_unknown_order_found_by_check_is_not_resent(env):
    env.fake.behaviours.append("network_after_create")
    res = await env.place()
    assert await env.svc.reconcile() == 1
    assert (await env.repo.get_order(res.order.uuid)).status == "accept"
    assert len(_new_orders(env)) == 1  # resolved by the check, no second newOrder


async def test_retry_error_never_refunds_and_alerts_owner(env):
    # First request lost before reaching the provider, then every retry is refused
    # (e.g. maintenance). The bot can't prove the order was never made: no refund.
    env.fake.behaviours.extend(["network"] + [("error", 130)] * 10)
    res = await env.place()
    assert res.order.status == "unknown"
    for _ in range(4):
        assert await env.svc.reconcile() == 0
    order = await env.repo.get_order(res.order.uuid)
    assert order.status == "unknown" and order.attempts == 5
    assert order.last_error == "api_130"
    assert await env.balance() == Decimal("8.79")  # still charged
    assert env.notified == []
    assert len(env.alerts) == 1 and "غير معروفة" in env.alerts[0]
    assert order.uuid in env.alerts[0]

    # The owner checks the provider panel and refunds by hand.
    refunded = await env.svc.owner_refund(order.uuid)
    assert refunded.status == "reject"
    assert await env.balance() == Decimal("10")


async def test_check_failure_keeps_order_unknown(env, monkeypatch):
    env.fake.behaviours.append("network_after_create")
    res = await env.place()

    async def down(uuids):
        raise G4SUnavailable("down")

    monkeypatch.setattr(env.api, "check_by_uuid", down)
    assert await env.svc.reconcile() == 0
    order = await env.repo.get_order(res.order.uuid)
    assert order.status == "unknown" and order.last_error == "check_network"
    assert order.attempts == 2
    assert len(_new_orders(env)) == 1  # not re-sent while the check is down
    assert await env.balance() == Decimal("8.79")


async def test_provider_reject_found_by_check_refunds(env):
    env.fake.behaviours.append("network_after_create")
    res = await env.place()
    env.fake.orders[res.order.uuid]["status"] = "reject"
    assert await env.svc.reconcile() == 1
    assert await env.balance() == Decimal("10")
    assert await env.svc.reconcile() == 0
    assert len(env.notified) == 1


async def test_order_keeps_the_field_labels(env):
    res = await env.place()
    assert res.order.labels == ["ادخل الايدي الاعب"]
    assert res.order.params == {"playerId": "PLAYER-ZX81Q"}
