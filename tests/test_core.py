from decimal import Decimal

import pytest

from bot.g4s import G4SClient, G4SError, G4SUnavailable, QtyRule
from bot.money import InvalidAmount, parse_user_amount, sell_total, to_decimal
from bot.security import Crypto, DecryptionError, generate_key
from tests.fakes import FakeG4S


# -- security ----------------------------------------------------------------
def test_encrypt_roundtrip_and_context_binding():
    c = Crypto(f"v1:{generate_key()}", generate_key())
    token = c.encrypt("123.45", "users/a/balance")
    assert "123.45" not in token
    assert c.decrypt(token, "users/a/balance") == "123.45"
    with pytest.raises(DecryptionError):
        c.decrypt(token, "users/b/balance")  # copied to another user


def test_key_rotation():
    k1, k2, h = generate_key(), generate_key(), generate_key()
    old = Crypto(f"v1:{k1}", h)
    token = old.encrypt("secret", "ctx")
    new = Crypto(f"v2:{k2},v1:{k1}", h)
    assert new.decrypt(token, "ctx") == "secret"
    assert new.needs_rotation(token)
    assert new.encrypt("x", "ctx").startswith("v2:")


def test_user_hash_is_stable_and_opaque():
    h = generate_key()
    a = Crypto(f"v1:{generate_key()}", h)
    b = Crypto(f"v1:{generate_key()}", h)
    assert a.user_hash(42) == b.user_hash(42)
    assert a.user_hash(42) != a.user_hash(43)
    assert len(a.user_hash(42)) == 64


def test_bad_keys_rejected():
    with pytest.raises(ValueError):
        Crypto("v1:short", generate_key())
    with pytest.raises(ValueError):
        Crypto(f"v1:{generate_key()}", "c2hvcnQ")


# -- money -------------------------------------------------------------------
def test_sell_total_rounds_up_and_avoids_float():
    assert sell_total(to_decimal(1.094), 1, Decimal("10")) == Decimal("1.21")  # 1.2034 -> 1.21
    assert sell_total(to_decimal(0.104), 1000, Decimal("10")) == Decimal("114.40")
    assert sell_total(Decimal("0.001"), 1, Decimal("0")) == Decimal("0.01")
    assert to_decimal(0.1) + to_decimal(0.2) == Decimal("0.3")


@pytest.mark.parametrize("raw,ok", [("10", "10.00"), ("٥٫٥", "5.50"), ("0.01", "0.01")])
def test_parse_user_amount_ok(raw, ok):
    assert parse_user_amount(raw) == Decimal(ok)


@pytest.mark.parametrize("raw", ["0", "-5", "1.234", "abc", "NaN", "inf"])
def test_parse_user_amount_bad(raw):
    with pytest.raises(InvalidAmount):
        parse_user_amount(raw)


# -- quantity rules ----------------------------------------------------------
def test_qty_rules():
    assert QtyRule.parse(None).validate(1) is None
    assert QtyRule.parse(None).validate(2)
    choices = QtyRule.parse(["110", "150", "210"])
    assert choices.choices == (110, 150, 210)
    assert choices.validate(150) is None and choices.validate(120)
    rng = QtyRule.parse({"min": "500", "max": "500000"})
    assert rng.validate(500) is None and rng.validate(499) and rng.validate(500001)


# -- API client --------------------------------------------------------------
@pytest.fixture
def fake():
    return FakeG4S()


@pytest.fixture
async def api(fake):
    client = G4SClient("test-token", transport=fake.transport())
    yield client
    await client.aclose()


async def test_profile_and_products(api):
    balance, _ = await api.profile()
    assert balance == Decimal("100.000")
    products = await api.products()
    assert {p.id for p in products} == {18, 365}
    assert (await api.products([18]))[0].price == Decimal("1.094")


async def test_error_codes(fake):
    client = G4SClient("wrong", transport=fake.transport())
    with pytest.raises(G4SError) as exc:
        await client.profile()
    assert exc.value.code == 121


async def test_new_order_sends_uuid_and_params(api, fake):
    res = await api.new_order(18, 1, "u-1", {"playerId": "555"})
    assert res.status == "accept"
    req = fake.requests[-1]
    assert req.url.path == "/client/api/newOrder/18/params"
    assert req.url.params["order_uuid"] == "u-1"
    assert req.url.params["playerId"] == "555"
    assert req.url.params["qty"] == "1"


async def test_transport_failures_are_unavailable(api, fake):
    fake.behaviours.extend(["network", "http500"])
    for _ in range(2):
        with pytest.raises(G4SUnavailable):
            await api.new_order(18, 1, "u-x", {"playerId": "1"})


async def test_check_by_uuid(api, fake):
    await api.new_order(18, 1, "u-2", {"playerId": "1"})
    found = await api.check_by_uuid(["u-2"])
    assert found["u-2"].status == "accept"
    assert fake.requests[-1].url.params["orders"] == "[u-2]"
    assert fake.requests[-1].url.params["uuid"] == "1"
