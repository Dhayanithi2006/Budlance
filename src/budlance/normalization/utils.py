"""Utility helpers for parsing prices, currencies, and numbers from external responses."""

import re
from decimal import Decimal
from typing import Any


def parse_price_and_currency(raw_value: Any, default_currency: str = "INR") -> tuple[Decimal, str]:
    """Extract a Decimal amount and currency code from raw numeric or string values.

    Handles formats like:
    - 4500 -> (Decimal("4500"), "INR")
    - "₹4,500" -> (Decimal("4500"), "INR")
    - "$120.50" -> (Decimal("120.50"), "USD")
    - "1500 INR" -> (Decimal("1500"), "INR")
    - "€80" -> (Decimal("80"), "EUR")
    """
    if raw_value is None:
        return Decimal("0.00"), default_currency

    if isinstance(raw_value, (int, float, Decimal)):
        return Decimal(str(raw_value)), default_currency

    clean = str(raw_value).strip()

    # Detect currency
    currency = default_currency
    if "$" in clean or "USD" in clean.upper():
        currency = "USD"
    elif "€" in clean or "EUR" in clean.upper():
        currency = "EUR"
    elif "£" in clean or "GBP" in clean.upper():
        currency = "GBP"
    elif "₹" in clean or "INR" in clean.upper() or "RS" in clean.upper():
        currency = "INR"

    # Check for negative value indicators (e.g. "-500", "-₹500", "₹-500")
    is_negative = bool(re.search(r"-\s*[\$€£₹A-Za-z]*[0-9]", clean))

    # Extract digits and optional decimal point
    num_match = re.search(r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)", clean)
    if num_match:
        sanitized_num = num_match.group(1).replace(",", "")
        if is_negative:
            sanitized_num = f"-{sanitized_num}"
        try:
            return Decimal(sanitized_num), currency
        except Exception:
            return Decimal("0.00"), currency

    return Decimal("0.00"), currency
