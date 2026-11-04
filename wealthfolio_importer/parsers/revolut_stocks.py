from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

from ..model import Activity, ConversionResult
from ..tabular import read_table
from ..utils import parse_decimal, source_hash


EXPECTED_HEADER = [
    "Date",
    "Ticker",
    "Type",
    "Quantity",
    "Price per share",
    "Total Amount",
    "Currency",
    "FX Rate",
]


def _money(value: str, currency: str, field: str) -> Decimal:
    match = re.fullmatch(r"([A-Z]{3})\s+(-?[0-9]+(?:\.[0-9]+)?)", value.strip())
    if match and match.group(1) == currency:
        return parse_decimal(match.group(2), field)
    if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value.strip()):
        return parse_decimal(value, field)
    if not match or match.group(1) != currency:
        raise ValueError(f"{field} no reconocido para {currency}: {value!r}")


def parse_revolut_stocks(path: Path, config: dict) -> ConversionResult:
    expected_currency = config.get("currency", "USD")
    header, values = read_table(path)
    if header != EXPECTED_HEADER:
        raise ValueError(
            "Cabecera de Revolut Stocks no reconocida. "
            f"Esperada: {EXPECTED_HEADER}; recibida: {header}"
        )
    source_rows = [dict(zip(header, row)) for row in values]
    skipped_currencies: Counter[str] = Counter()
    rows = []
    for row in source_rows:
        currency = row["Currency"].strip()
        if currency != expected_currency:
            skipped_currencies[currency or "VACÍA"] += 1
            continue
        rows.append(row)

    if not rows:
        raise ValueError(
            f"El extracto de Revolut Stocks no contiene actividades en {expected_currency}"
        )

    activities: list[Activity] = []
    counts: dict[str, int] = {}

    for line_number, row in enumerate(rows, 2):
        currency = row["Currency"].strip()
        if currency != expected_currency:
            raise ValueError(
                f"Línea {line_number}: divisa {currency!r}; se esperaba {expected_currency!r}"
            )
        kind = row["Type"].strip()
        amount_signed = _money(row["Total Amount"], currency, "Total Amount")
        quantity = parse_decimal(row["Quantity"], "Quantity") if row["Quantity"].strip() else None
        unit_price = (
            _money(row["Price per share"], currency, "Price per share")
            if row["Price per share"].strip()
            else None
        )
        reported_fx = parse_decimal(row["FX Rate"], "FX Rate")
        if reported_fx <= 0:
            raise ValueError(f"Línea {line_number}: FX Rate debe ser positivo")
        fx_rate = (Decimal("1") / reported_fx).quantize(Decimal("0.0000000001"))
        ref = source_hash(*(row[column] for column in EXPECTED_HEADER))
        symbol = row["Ticker"].strip()
        common = {
            "date": row["Date"].strip(),
            "currency": currency,
            "source_ref": f"revolut-stocks:{ref}",
            "fx_rate": fx_rate,
        }

        if kind == "CASH TOP-UP":
            activity = Activity(
                **common,
                activity_type="DEPOSIT",
                amount=abs(amount_signed),
                comment="Revolut: CASH TOP-UP",
            )
        elif kind == "CASH WITHDRAWAL":
            activity = Activity(
                **common,
                activity_type="WITHDRAWAL",
                amount=abs(amount_signed),
                comment="Revolut: CASH WITHDRAWAL",
            )
        elif kind in {"BUY - MARKET", "SELL - MARKET"}:
            if not symbol or quantity is None or unit_price is None:
                raise ValueError(f"Línea {line_number}: operación {kind} incompleta")
            activity = Activity(
                **common,
                activity_type="BUY" if kind.startswith("BUY") else "SELL",
                amount=abs(amount_signed),
                symbol=symbol,
                instrument_type="EQUITY",
                quantity=abs(quantity),
                unit_price=unit_price,
                comment=f"Revolut: {kind}",
            )
        elif kind == "DIVIDEND":
            if not symbol:
                raise ValueError(f"Línea {line_number}: dividendo sin ticker")
            activity = Activity(
                **common,
                activity_type="DIVIDEND",
                amount=abs(amount_signed),
                symbol=symbol,
                instrument_type="EQUITY",
                comment="Revolut: DIVIDEND",
            )
        elif kind == "DIVIDEND TAX (CORRECTION)":
            is_refund = amount_signed > 0
            activity = Activity(
                **common,
                activity_type="CREDIT" if is_refund else "TAX",
                amount=abs(amount_signed),
                symbol=symbol,
                instrument_type="EQUITY" if symbol else "",
                subtype="REIMBURSEMENT" if is_refund else "",
                comment=(
                    "Revolut: dividend tax correction (refund)"
                    if is_refund
                    else "Revolut: dividend tax correction (charge)"
                ),
            )
        else:
            raise ValueError(f"Línea {line_number}: tipo de Revolut desconocido: {kind!r}")

        counts[activity.activity_type] = counts.get(activity.activity_type, 0) + 1
        activities.append(activity)

    return ConversionResult(
        activities=activities,
        checks={
            "sourceRows": len(source_rows),
            "completedRows": len(rows),
            "skippedCurrencyRows": sum(skipped_currencies.values()),
            "activityCounts": counts,
        },
        warnings=(
            [
                f"Filas de otras divisas omitidas para {expected_currency}: "
                + ", ".join(
                    f"{currency}={count}"
                    for currency, count in sorted(skipped_currencies.items())
                )
            ]
            if skipped_currencies
            else []
        ),
    ).sorted()
