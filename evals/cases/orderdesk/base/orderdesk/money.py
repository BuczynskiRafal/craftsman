"""Whole-cent money helpers."""

from __future__ import annotations


def to_cents(amount: str) -> int:
    """Parse a decimal amount such as '12.5' or '-3.05' into cents."""
    text = amount.strip()
    sign = -1 if text.startswith("-") else 1
    whole, _, frac = text.lstrip("-").partition(".")
    if not whole.isdigit() or len(frac) > 2 or (frac and not frac.isdigit()):
        raise ValueError(f"not a money amount: {amount!r}")
    return sign * (int(whole) * 100 + int((frac + "00")[:2]))


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def percent_of(cents: int, basis_points: int) -> int:
    """cents * basis_points / 10000, rounded half up."""
    return (cents * basis_points + 5000) // 10000
