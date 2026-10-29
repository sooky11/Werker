from decimal import Decimal
from pathlib import Path

import pytest

from werker.prices import (
    PriceBook,
    PriceError,
    find_amounts,
    format_price_list,
    load_csv,
    load_yaml,
    parse_amount,
    validate_prices,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "prices.example.yaml"


def test_parse_amount_accepts_dutch_and_dot_notation():
    assert parse_amount("25,00") == Decimal("25.00")
    assert parse_amount("€ 1.250,5") == Decimal("1250.50")
    assert parse_amount("25.50") == Decimal("25.50")
    assert parse_amount("1.250") == Decimal("1250.00")


def test_parse_amount_rejects_garbage():
    with pytest.raises(PriceError):
        parse_amount("veel")


def test_find_amounts_recognises_euro_sign_and_word():
    assert find_amounts("Een knipbeurt kost € 25,00 en kleuren 65 euro.") == [
        Decimal("25.00"),
        Decimal("65.00"),
    ]


def test_find_amounts_ignores_plain_numbers():
    assert find_amounts("We zijn open van 9 tot 17 uur, tel. 0612345678.") == []


def test_validate_prices_blocks_invented_amount():
    prices = load_yaml(EXAMPLE)
    errors = validate_prices("Dat kost € 30,00.", prices)
    assert errors == ["bedrag € 30,00 staat niet in de prijslijst"]


def test_validate_prices_passes_known_amount():
    prices = load_yaml(EXAMPLE)
    assert validate_prices("Een herenknipbeurt is € 25,00.", prices) == []


def test_inactive_rows_are_skipped():
    prices = load_yaml(EXAMPLE)
    assert "Oud tarief" not in [p.product for p in prices]


def test_csv_loader_reads_sheet_export():
    text = "Product,Prijs,Eenheid,Actief\nKleuren,\"65,00\",behandeling,ja\nOud,10,,nee\n"
    prices = load_csv(text)
    assert [(p.product, p.amount) for p in prices] == [("Kleuren", Decimal("65.00"))]


def test_missing_product_raises_with_row_number():
    with pytest.raises(PriceError, match="rij 2"):
        load_csv("Product,Prijs\n,25\n")


def test_empty_list_raises():
    with pytest.raises(PriceError, match="leeg"):
        load_csv("Product,Prijs,Actief\nKleuren,65,nee\n")


def test_format_price_list_is_readable_for_model():
    text = format_price_list(load_yaml(EXAMPLE))
    assert "- Herenknipbeurt: € 25,00 per behandeling (inclusief wassen)" in text


def test_pricebook_keeps_previous_version_on_failed_refresh():
    state = {"fail": False}

    def loader():
        if state["fail"]:
            raise OSError("sheet niet bereikbaar")
        return load_yaml(EXAMPLE)

    book = PriceBook(loader)
    assert book.refresh() is True
    count = len(book.prices)
    state["fail"] = True
    assert book.refresh() is False
    assert len(book.prices) == count
