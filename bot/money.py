"""Money helpers. All amounts are Decimal; floats are never used for money."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0")


class InvalidAmount(ValueError):
    pass


def to_decimal(value) -> Decimal:
    """Convert API values (str/int/float) to Decimal without float artefacts."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        value = repr(value)
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise InvalidAmount(f"Not a valid amount: {value!r}") from exc
    if not result.is_finite():
        raise InvalidAmount(f"Not a finite amount: {value!r}")
    return result


def parse_user_amount(text: str) -> Decimal:
    """Parse an amount typed by the owner: positive, max 2 decimals."""
    text = text.strip().replace("٫", ".").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    amount = to_decimal(text)
    if amount <= 0:
        raise InvalidAmount("Amount must be positive")
    if amount != amount.quantize(CENT):
        raise InvalidAmount("Amount can have at most 2 decimal places")
    return amount.quantize(CENT)


def sell_total(unit_cost: Decimal, qty: int, margin_percent: Decimal) -> Decimal:
    """Customer price: cost * qty * (1 + margin), rounded UP to the cent so the
    shop never loses fractions of a cent."""
    raw = unit_cost * qty * (Decimal(1) + margin_percent / Decimal(100))
    total = raw.quantize(CENT, rounding=ROUND_UP)
    return max(total, CENT)


def fmt(amount: Decimal, symbol: str = "$") -> str:
    return f"{amount.quantize(CENT, rounding=ROUND_HALF_UP)} {symbol}".strip()
