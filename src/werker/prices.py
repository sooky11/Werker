from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

log = logging.getLogger(__name__)

CENT = Decimal("0.01")
INACTIVE_VALUES = frozenset({"nee", "no", "false", "0"})
_AMOUNT_RE = re.compile(r"€\s?(\d[\d.,]*\d|\d)|(\d[\d.,]*\d|\d)\s?(?:euro|EUR)\b", re.IGNORECASE)


class PriceError(ValueError):
    pass


@dataclass(frozen=True)
class Price:
    product: str
    amount: Decimal
    unit: str = ""
    note: str = ""


def parse_amount(raw: str) -> Decimal:
    text = raw.strip().replace("€", "").replace("EUR", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")
    try:
        return Decimal(text).quantize(CENT)
    except InvalidOperation as exc:
        raise PriceError(f"ongeldig bedrag: {raw!r}") from exc


def format_amount(amount: Decimal) -> str:
    return f"{amount:.2f}".replace(".", ",")


def find_amounts(text: str) -> list[Decimal]:
    amounts = []
    for match in _AMOUNT_RE.finditer(text):
        raw = match.group(1) or match.group(2)
        amounts.append(parse_amount(raw.rstrip(".,")))
    return amounts


def validate_prices(text: str, prices: list[Price]) -> list[str]:
    known = {price.amount for price in prices}
    return [
        f"bedrag € {format_amount(amount)} staat niet in de prijslijst"
        for amount in find_amounts(text)
        if amount not in known
    ]


def format_price_list(prices: list[Price]) -> str:
    if not prices:
        return "(geen prijzen beschikbaar; noem geen bedragen)"
    lines = []
    for price in prices:
        line = f"- {price.product}: € {format_amount(price.amount)}"
        if price.unit:
            line += f" per {price.unit}"
        if price.note:
            line += f" ({price.note})"
        lines.append(line)
    return "\n".join(lines)


def parse_rows(rows: list[dict[str, Any]]) -> list[Price]:
    prices = []
    for number, raw_row in enumerate(rows, start=2):
        row = {str(k).strip().lower(): str(v or "").strip() for k, v in raw_row.items() if k}
        if row.get("actief", "ja").lower() in INACTIVE_VALUES:
            continue
        product = row.get("product", "")
        if not product:
            raise PriceError(f"rij {number}: product ontbreekt")
        try:
            amount = parse_amount(row.get("prijs", ""))
        except PriceError as exc:
            raise PriceError(f"rij {number}: {exc}") from exc
        if amount < 0:
            raise PriceError(f"rij {number}: negatief bedrag")
        prices.append(Price(product, amount, row.get("eenheid", ""), row.get("toelichting", "")))
    if not prices:
        raise PriceError("prijslijst is leeg")
    return prices


def load_csv(text: str) -> list[Price]:
    return parse_rows(list(csv.DictReader(io.StringIO(text))))


def load_yaml(path: Path) -> list[Price]:
    return parse_rows(yaml.safe_load(path.read_text(encoding="utf-8")) or [])


def fetch_sheet_csv(url: str) -> str:
    response = httpx.get(url, follow_redirects=True, timeout=20)
    response.raise_for_status()
    if "text/csv" not in response.headers.get("content-type", ""):
        raise PriceError("sheet is niet als CSV bereikbaar; deel de sheet met 'iedereen met de link'")
    return response.text


class PriceBook:
    """Houdt de prijslijst vast. Een mislukte verversing laat de vorige versie staan."""

    def __init__(self, loader: Callable[[], list[Price]]):
        self._loader = loader
        self._prices: list[Price] = []

    def refresh(self) -> bool:
        try:
            self._prices = self._loader()
        except Exception:
            log.exception("prijslijst niet bijgewerkt; vorige versie blijft actief")
            return False
        return True

    @property
    def prices(self) -> list[Price]:
        return list(self._prices)
