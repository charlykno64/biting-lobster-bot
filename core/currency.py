from __future__ import annotations

import re
from typing import Any


class CurrencyConverter:
    def __init__(self, config: dict[str, Any]) -> None:
        criteria = config.get("search_criteria", {})
        currency_rates = criteria.get("currency_rates", {})
        self.base_currency = currency_rates.get("base_currency", "USD")
        self.rates = currency_rates.get("rates", {"USD": 1.0})

    def to_usd_cents(self, amount: float, source_currency: str) -> int:
        source = source_currency.upper()
        rate = self.rates.get(source)
        if rate is None or rate <= 0:
            raise ValueError(f"No exchange rate configured for {source}.")
        usd_amount = amount / float(rate)
        return int(round(usd_amount * 100))

    def parse_price_text(self, price_text: str) -> tuple[float, str]:
        upper = price_text.upper()
        detected_currency = "USD"
        for code in ("USD", "MXN", "CAD"):
            if code in upper:
                detected_currency = code
                break

        numeric_part = re.sub(r"[^0-9.,]", "", price_text)
        normalized = numeric_part.replace(",", "")
        return float(normalized), detected_currency
