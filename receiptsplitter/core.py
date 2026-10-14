from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from rapidfuzz import fuzz, process

ITEM_LINE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9\-\s&'\./()]*?)\s+"
    r"(?P<price>[-+$]?\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*$"
)
QTY_HINT_RE = re.compile(r"\b(?:(?:x|qty)\s*(?P<qty>\d+)|(?P<qty2>\d+)\s*x)\b", re.IGNORECASE)
SUMMARY_LINE_RE = re.compile(
    r"^\s*(?P<label>sub\s*total|sales\s+tax|tax|vat|gst|hst|grand\s+total|"
    r"amount\s+due|balance\s+due|total)\s*:?\s*"
    r"(?P<amount>[-+]?(?:\$\s*)?\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*$",
    re.IGNORECASE,
)
MONEY = Decimal("0.01")
MAX_ITEM_LENGTH = 200
MAX_QUANTITY = 999
MAX_LINE_TOTAL = Decimal("1000000.00")


class ReceiptCorrectionError(ValueError):
    """A requested correction does not satisfy the receipt row contract."""


def _money(value: Any) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as cause:
        raise ValueError("Use a valid decimal money amount.") from cause
    if not result.is_finite():
        raise ValueError("Use a finite decimal money amount.")
    return result


def _to_decimal(value: str) -> Decimal:
    cleaned = value.replace(",", "").replace("$", "").replace("USD", "").strip()
    return _money(cleaned.removeprefix("+"))


def _summary_key(label: str) -> str:
    normalized = re.sub(r"\s+", " ", label.casefold()).strip()
    if normalized in {"sub total", "subtotal"}:
        return "subtotal"
    if normalized in {"tax", "sales tax", "vat", "gst", "hst"}:
        return "tax"
    return "total"


@dataclass
class ReceiptRow:
    item_id: int
    item: str
    qty: int
    line_total: Decimal
    tax_applies: bool = True
    who: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "item": self.item,
            "qty": self.qty,
            "line_total": float(self.line_total),
            "tax_applies": self.tax_applies,
            "who": self.who,
        }


def parse_receipt_lines(lines: list[str]) -> tuple[list[ReceiptRow], dict[str, float]]:
    """Parse explicit summary rows and item line totals without inferring unit prices."""
    expected: dict[str, Decimal] = {}
    items: list[ReceiptRow] = []

    for line in lines:
        summary = SUMMARY_LINE_RE.fullmatch(line)
        if summary:
            expected[_summary_key(summary.group("label"))] = _to_decimal(summary.group("amount"))

    for line in lines:
        if SUMMARY_LINE_RE.fullmatch(line):
            continue
        match = ITEM_LINE_RE.fullmatch(line)
        if not match:
            continue
        name = match.group("name").strip()
        qty = 1
        qty_match = QTY_HINT_RE.search(name)
        if qty_match:
            qty = int(qty_match.group("qty") or qty_match.group("qty2"))
            name = QTY_HINT_RE.sub("", name).strip()
        if not name or qty < 1 or qty > MAX_QUANTITY:
            continue
        items.append(
            ReceiptRow(
                item_id=len(items),
                item=name,
                qty=qty,
                # Receipt conventions differ: a trailing amount is retained as the line total.
                # We never multiply it by quantity or infer an unprinted unit price.
                line_total=_to_decimal(match.group("price")),
            )
        )

    return items, {key: float(value) for key, value in expected.items()}


def _allocate_cents(total_cents: int, weights: dict[str, int]) -> dict[str, int]:
    if total_cents <= 0 or not weights or sum(weights.values()) <= 0:
        return {name: 0 for name in weights}
    weight_total = sum(weights.values())
    raw = {
        name: Decimal(total_cents * weight) / Decimal(weight_total)
        for name, weight in weights.items()
    }
    allocated = {name: int(value) for name, value in raw.items()}
    remaining = total_cents - sum(allocated.values())
    order = sorted(raw, key=lambda name: (raw[name] - allocated[name], name), reverse=True)
    for name in order[:remaining]:
        allocated[name] += 1
    return allocated


@dataclass
class ReceiptState:
    df: list[ReceiptRow] = field(default_factory=list)
    expected: dict[str, float] = field(default_factory=dict)
    tax_included: bool = False

    def _line_cents(self) -> list[int]:
        return [int(row.line_total * 100) for row in self.df]

    def compute_totals(self) -> dict[str, float]:
        subtotal_cents = sum(self._line_cents())
        if self.tax_included:
            tax_cents = 0
            total_cents = subtotal_cents
        else:
            tax_cents = int(_money(self.expected.get("tax", 0)) * 100)
            total_cents = subtotal_cents + tax_cents
        return {
            "subtotal": float(Decimal(subtotal_cents) / 100),
            "tax": float(Decimal(tax_cents) / 100),
            "total": float(Decimal(total_cents) / 100),
        }

    def validation_report(self) -> dict[str, Any]:
        calculated = self.compute_totals()
        issues: list[str] = []
        if "total" not in self.expected:
            issues.append("The receipt total was not recognized. Try a clearer, straight-on photo.")
        keys = ("total",) if self.tax_included else ("subtotal", "tax", "total")
        for key in keys:
            if key in self.expected and _money(calculated[key]) != _money(self.expected[key]):
                issues.append(
                    f"{key} mismatch: parsed {calculated[key]:.2f}; "
                    f"receipt {self.expected[key]:.2f}"
                )
        if not self.df:
            issues.append("No line items were recognized. Try a sharper, straight-on photo.")
        return {
            "calculated": calculated,
            "expected": self.expected,
            "ok": not issues,
            "issues": issues,
            "needs_review": bool(issues),
        }

    def to_markdown(self) -> str:
        if not self.df:
            return "(No items parsed)"
        headers = ("item_id", "item", "qty", "line_total", "tax_applies", "who")
        rows = [
            (
                str(row.item_id),
                row.item.replace("|", "\\|"),
                str(row.qty),
                f"{row.line_total:.2f}",
                str(row.tax_applies),
                row.who.replace("|", "\\|"),
            )
            for row in self.df
        ]
        return "\n".join(
            [
                "| " + " | ".join(headers) + " |",
                "| " + " | ".join("---" for _ in headers) + " |",
                *("| " + " | ".join(row) + " |" for row in rows),
            ]
        )

    def assign(self, assignments: dict[str, str]) -> list[str]:
        unresolved: list[str] = []
        names = [row.item for row in self.df]
        for key, raw_person in assignments.items():
            person = str(raw_person).strip()
            if not person:
                unresolved.append(str(key))
                continue
            key_text = str(key).strip()
            if key_text.isdigit():
                row = self._row_for_id(int(key_text))
                if row is not None:
                    row.who = person
                    continue
            exact = [
                index for index, name in enumerate(names) if name.casefold() == key_text.casefold()
            ]
            if len(exact) == 1:
                self.df[exact[0]].who = person
                continue
            matches = process.extract(key_text, names, scorer=fuzz.WRatio, limit=2)
            if (
                not matches
                or matches[0][1] < 85
                or (len(matches) > 1 and matches[1][1] == matches[0][1])
            ):
                unresolved.append(key_text)
                continue
            self.df[names.index(matches[0][0])].who = person
        return unresolved

    def correct_row(
        self,
        item_id: int,
        *,
        item: str | None = None,
        quantity: int | None = None,
        line_total: str | None = None,
        tax_applies: bool | None = None,
    ) -> ReceiptRow:
        row = self._row_for_id(item_id)
        if row is None:
            raise ReceiptCorrectionError("No receipt row has that item ID.")

        corrected_item = row.item
        corrected_quantity = row.qty
        corrected_line_total = row.line_total
        corrected_tax_applies = row.tax_applies
        if item is not None:
            if not isinstance(item, str):
                raise ReceiptCorrectionError("Item text must be a string.")
            cleaned = item.strip()
            if not cleaned or len(cleaned) > MAX_ITEM_LENGTH:
                raise ReceiptCorrectionError("Item text must contain 1 to 200 characters.")
            corrected_item = cleaned
        if quantity is not None:
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or not 1 <= quantity <= MAX_QUANTITY
            ):
                raise ReceiptCorrectionError("Quantity must be an integer from 1 to 999.")
            corrected_quantity = quantity
        if line_total is not None:
            if not isinstance(line_total, str):
                raise ReceiptCorrectionError("Line total must be a decimal string.")
            try:
                corrected_total = _to_decimal(line_total)
            except ValueError as cause:
                raise ReceiptCorrectionError(str(cause)) from cause
            if abs(corrected_total) > MAX_LINE_TOTAL:
                raise ReceiptCorrectionError("Line total exceeds the supported limit.")
            corrected_line_total = corrected_total
        if tax_applies is not None:
            if not isinstance(tax_applies, bool):
                raise ReceiptCorrectionError("Tax applicability must be true or false.")
            corrected_tax_applies = tax_applies

        row.item = corrected_item
        row.qty = corrected_quantity
        row.line_total = corrected_line_total
        row.tax_applies = corrected_tax_applies
        return row

    def _row_for_id(self, item_id: int) -> ReceiptRow | None:
        return next((row for row in self.df if row.item_id == item_id), None)

    def per_person_totals(self) -> dict[str, dict[str, float]]:
        base: dict[str, int] = {}
        taxable: dict[str, int] = {}
        for row, cents in zip(self.df, self._line_cents(), strict=True):
            person = row.who.strip() or "(unassigned)"
            base[person] = base.get(person, 0) + cents
            if row.tax_applies:
                taxable[person] = taxable.get(person, 0) + cents
        tax_total = 0 if self.tax_included else int(_money(self.expected.get("tax", 0)) * 100)
        tax_by_person = _allocate_cents(tax_total, taxable)
        return {
            person: {
                "subtotal": float(Decimal(cents) / 100),
                "tax": float(Decimal(tax_by_person.get(person, 0)) / 100),
                "total": float(Decimal(cents + tax_by_person.get(person, 0)) / 100),
            }
            for person, cents in base.items()
        }

    def split_reconciliation(self) -> dict[str, float | bool | None]:
        expected_total = self.expected.get("total")
        per_person_cents = sum(
            int(_money(amounts["total"]) * 100) for amounts in self.per_person_totals().values()
        )
        expected_cents = int(_money(expected_total) * 100) if expected_total is not None else None
        difference_cents = per_person_cents - expected_cents if expected_cents is not None else None
        return {
            "expected_total": expected_total,
            "per_person_total": float(Decimal(per_person_cents) / 100),
            "difference": (
                float(Decimal(difference_cents) / 100) if difference_cents is not None else None
            ),
            "ok": difference_cents == 0 if difference_cents is not None else False,
        }
