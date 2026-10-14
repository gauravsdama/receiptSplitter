from pathlib import Path

import pytest

from receiptsplitter.core import ReceiptCorrectionError, ReceiptState, parse_receipt_lines


def test_fixture_parses_and_reconciles() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "sanitized_receipt.txt"
    table, expected = parse_receipt_lines(fixture.read_text(encoding="utf-8").splitlines())
    state = ReceiptState(df=table, expected=expected)

    assert [row.as_dict() for row in table] == [
        {
            "item_id": 0,
            "item": "Apples",
            "qty": 1,
            "line_total": 3.0,
            "tax_applies": True,
            "who": "",
        },
        {
            "item_id": 1,
            "item": "Bread",
            "qty": 1,
            "line_total": 2.0,
            "tax_applies": True,
            "who": "",
        },
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
    assert [row.who for row in state.df] == ["", ""]
    assert state.assign({"0": "Sam", "1": "Lee"}) == []
    assert [row.who for row in state.df] == ["Sam", "Lee"]


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


def test_missing_receipt_total_needs_a_new_photo() -> None:
    table, expected = parse_receipt_lines(["Tea 2.00"])
    state = ReceiptState(df=table, expected=expected)

    assert state.validation_report()["needs_review"] is True
    assert "total was not recognized" in state.validation_report()["issues"][0]


def test_summary_labels_are_anchored_and_do_not_consume_items() -> None:
    rows, expected = parse_receipt_lines(
        [
            "Total Cereal 4.00",
            "Tax Free Tea 2.00",
            "TOTAL SAVINGS 1.00",
            "SUB TOTAL: $6.00",
            "SALES TAX: $0.48",
            "AMOUNT DUE: $6.48",
        ]
    )

    assert [row.item for row in rows] == ["Total Cereal", "Tax Free Tea", "TOTAL SAVINGS"]
    assert expected == {"subtotal": 6.0, "tax": 0.48, "total": 6.48}


def test_quantity_is_metadata_and_trailing_amount_is_line_total() -> None:
    rows, expected = parse_receipt_lines(
        ["Soda qty 3 6.00", "SUBTOTAL 6.00", "TAX 0.00", "TOTAL 6.00"]
    )
    state = ReceiptState(df=rows, expected=expected)

    assert rows[0].qty == 3
    assert rows[0].line_total == 6
    assert state.compute_totals()["subtotal"] == 6.0


def test_correction_revalidates_and_final_split_reconciles() -> None:
    rows, expected = parse_receipt_lines(["Tea 3.00", "SUBTOTAL 2.00", "TAX 0.20", "TOTAL 2.20"])
    state = ReceiptState(df=rows, expected=expected)
    assert state.validation_report()["ok"] is False

    corrected = state.correct_row(0, item="Tea", quantity=2, line_total="2.00")
    corrected.who = "Sam"

    assert corrected.qty == 2
    assert state.validation_report()["ok"] is True
    assert state.split_reconciliation() == {
        "expected_total": 2.2,
        "per_person_total": 2.2,
        "difference": 0.0,
        "ok": True,
    }


def test_invalid_multi_field_correction_is_atomic() -> None:
    rows, expected = parse_receipt_lines(["Tea 2.00", "TOTAL 2.00"])
    state = ReceiptState(df=rows, expected=expected, tax_included=True)

    with pytest.raises(ReceiptCorrectionError, match="valid decimal"):
        state.correct_row(0, item="Changed", line_total="not-money")

    assert rows[0].item == "Tea"
    assert rows[0].line_total == 2
