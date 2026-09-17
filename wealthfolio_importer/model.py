from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


CSV_COLUMNS = [
    "date",
    "symbol",
    "instrumentType",
    "isin",
    "quantity",
    "activityType",
    "unitPrice",
    "currency",
    "fee",
    "tax",
    "amount",
    "fxRate",
    "subtype",
    "comment",
]


def decimal_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


@dataclass(frozen=True)
class Activity:
    date: str
    activity_type: str
    currency: str
    amount: Decimal
    source_ref: str
    symbol: str = ""
    instrument_type: str = ""
    isin: str = ""
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    fee: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    fx_rate: Decimal | None = None
    subtype: str = ""
    comment: str = ""

    @property
    def identifier(self) -> str:
        raw = "|".join(
            [
                self.source_ref,
                self.date,
                self.activity_type,
                self.currency,
                self.symbol,
                decimal_text(self.quantity),
                decimal_text(self.unit_price),
                decimal_text(self.amount),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def as_row(self) -> dict[str, str]:
        return {
            "date": self.date,
            "symbol": self.symbol,
            "instrumentType": self.instrument_type,
            "isin": self.isin,
            "quantity": decimal_text(self.quantity),
            "activityType": self.activity_type,
            "unitPrice": decimal_text(self.unit_price),
            "currency": self.currency,
            "fee": decimal_text(self.fee),
            "tax": decimal_text(self.tax),
            "amount": decimal_text(self.amount),
            "fxRate": decimal_text(self.fx_rate),
            "subtype": self.subtype,
            "comment": self.comment,
        }


@dataclass
class ConversionResult:
    activities: list[Activity]
    checks: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def sorted(self) -> "ConversionResult":
        self.activities.sort(key=lambda item: (item.date, item.source_ref))
        return self


def csv_text(activities: list[Activity]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(activity.as_row() for activity in activities)
    return buffer.getvalue()


def write_csv(path: Path, activities: list[Activity]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(csv_text(activities), encoding="utf-8", newline="")
