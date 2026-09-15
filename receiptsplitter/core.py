from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pandas as pd
from rapidfuzz import fuzz, process

ITEM_LINE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9\-\s&'\./()]*?)\s+"
    r"(?P<price>[-+$]?\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*$"
)
QTY_HINT_RE = re.compile(r"\b(?:(?:x|qty)\s*(?P<qty>\d+)|(?P<qty2>\d+)\s*x)\b", re.IGNORECASE)
TOTAL_KEYS = ("total", "grand total", "amount due", "balance due")
SUBTOTAL_KEYS = ("subtotal", "sub total")
TAX_KEYS = ("tax", "sales tax", "vat", "gst", "hst")
MONEY = Decimal("0.01")


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _to_decimal(value: str) -> Decimal:
    cleaned = value.replace(",", "").replace("$", "").replace("USD", "").strip()
    return _money(cleaned.removeprefix("+"))


def parse_receipt_lines(lines: list[str]) -> tuple[pd.DataFrame, dict[str, float]]:
    expected: dict[str, Decimal] = {}
    items: list[dict[str, Any]] = []

    for line in lines:
        lowered = line.lower()
        for keys, label in ((SUBTOTAL_KEYS, "subtotal"), (TAX_KEYS, "tax"), (TOTAL_KEYS, "total")):
            if any(key in lowered for key in keys):
                values = re.findall(r"(-?\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", line)
                if values:
                    expected[label] = _to_decimal(values[-1])
                break

    for line in lines:
        match = ITEM_LINE_RE.match(line)
        if not match:
            continue
        name = match.group("name").strip()
        lowered = name.lower()
        if any(key in lowered for key in (*SUBTOTAL_KEYS, *TAX_KEYS, *TOTAL_KEYS)):
            continue
        qty = 1
        qty_match = QTY_HINT_RE.search(name)
        if qty_match:
            qty = int(qty_match.group("qty") or qty_match.group("qty2"))
            name = QTY_HINT_RE.sub("", name).strip()
        items.append(
            {
                "item_id": len(items),
                "item": name,
                "qty": qty,
                "price": float(_to_decimal(match.group("price"))),
                "tax_applies": True,
                "who": "",
            }
        )

    columns = ["item_id", "item", "qty", "price", "tax_applies", "who"]
    return pd.DataFrame(items, columns=columns), {
        key: float(value) for key, value in expected.items()
    }


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
    df: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(
            columns=["item_id", "item", "qty", "price", "tax_applies", "who"]
        )
    )
    expected: dict[str, float] = field(default_factory=dict)
    tax_included: bool = False

    def _line_cents(self) -> list[int]:
        return [int(_money(row.qty * row.price) * 100) for row in self.df.itertuples()]

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
        if self.df.empty:
            issues.append("No line items were recognized. Try a sharper, straight-on photo.")
        return {
            "calculated": calculated,
            "expected": self.expected,
            "ok": not issues,
            "issues": issues,
            "needs_review": bool(issues),
        }

    def to_markdown(self) -> str:
        if self.df.empty:
            return "(No items parsed)"
        display = self.df.copy()
        display["line_total"] = [float(Decimal(cents) / 100) for cents in self._line_cents()]
        return display.to_markdown(index=False)

    def assign(self, assignments: dict[str, str]) -> list[str]:
        unresolved: list[str] = []
        names = list(self.df["item"].astype(str))
        for key, raw_person in assignments.items():
            person = str(raw_person).strip()
            if not person:
                unresolved.append(str(key))
                continue
            key_text = str(key).strip()
            if key_text.isdigit() and int(key_text) in set(self.df["item_id"]):
                self.df.loc[self.df["item_id"] == int(key_text), "who"] = person
                continue
            exact = [
                index for index, name in enumerate(names) if name.casefold() == key_text.casefold()
            ]
            if len(exact) == 1:
                self.df.at[exact[0], "who"] = person
                continue
            matches = process.extract(key_text, names, scorer=fuzz.WRatio, limit=2)
            if (
                not matches
                or matches[0][1] < 85
                or (len(matches) > 1 and matches[1][1] == matches[0][1])
            ):
                unresolved.append(key_text)
                continue
            self.df.at[names.index(matches[0][0]), "who"] = person
        return unresolved

    def per_person_totals(self) -> dict[str, dict[str, float]]:
        base: dict[str, int] = {}
        taxable: dict[str, int] = {}
        for row, cents in zip(self.df.itertuples(), self._line_cents(), strict=True):
            person = str(row.who).strip() or "(unassigned)"
            base[person] = base.get(person, 0) + cents
            if bool(row.tax_applies):
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
