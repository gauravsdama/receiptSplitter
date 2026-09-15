from pathlib import Path

from receiptsplitter.core import ReceiptState, parse_receipt_lines


def test_fixture_parses_and_reconciles() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "sanitized_receipt.txt"
    table, expected = parse_receipt_lines(fixture.read_text(encoding="utf-8").splitlines())
    state = ReceiptState(df=table, expected=expected)

    assert table[["item_id", "item", "qty", "price"]].to_dict("records") == [
        {"item_id": 0, "item": "Apples", "qty": 1, "price": 3.0},
        {"item_id": 1, "item": "Bread", "qty": 1, "price": 2.0},
    ]
    assert state.validation_report() == {
        "calculated": {"subtotal": 5.0, "tax": 0.4, "total": 5.4},
        "expected": {"subtotal": 5.0, "tax": 0.4, "total": 5.4},
        "ok": True,
        "issues": [],
        "needs_review": False,
    }


def test_duplicate_names_require_row_ids() -> None:
    table, expected = parse_receipt_lines(
        ["Tea 1.00", "Tea 1.00", "SUBTOTAL 2.00", "TAX 0.00", "TOTAL 2.00"]
    )
    state = ReceiptState(df=table, expected=expected)

    assert state.assign({"Tea": "Sam"}) == ["Tea"]
    assert list(state.df["who"]) == ["", ""]
    assert state.assign({"0": "Sam", "1": "Lee"}) == []
    assert list(state.df["who"]) == ["Sam", "Lee"]


def test_tax_cents_reconcile_exactly() -> None:
    table, expected = parse_receipt_lines(
        ["A 1.00", "B 1.00", "C 1.00", "SUBTOTAL 3.00", "TAX 0.01", "TOTAL 3.01"]
    )
    state = ReceiptState(df=table, expected=expected)
    state.assign({"0": "Ari", "1": "Bea", "2": "Cal"})

    totals = state.per_person_totals()
    assert sum(round(value["tax"] * 100) for value in totals.values()) == 1
    assert sum(round(value["total"] * 100) for value in totals.values()) == 301


def test_tax_included_receipt_uses_line_total() -> None:
    table, expected = parse_receipt_lines(["Meal 10.00", "TOTAL 10.00"])
    state = ReceiptState(df=table, expected=expected, tax_included=True)

    assert state.compute_totals() == {"subtotal": 10.0, "tax": 0.0, "total": 10.0}
    assert state.validation_report()["ok"] is True


def test_empty_or_mismatched_receipt_needs_review() -> None:
    empty = ReceiptState()
    assert empty.validation_report()["needs_review"] is True

    table, expected = parse_receipt_lines(["Tea 2.00", "TOTAL 3.00"])
    mismatch = ReceiptState(df=table, expected=expected, tax_included=True)
    assert mismatch.validation_report()["ok"] is False
