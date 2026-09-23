from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..model import Activity, ConversionResult
from ..utils import (
    iso_local,
    parse_european_decimal,
    parse_revolut_local_date,
    source_hash,
)


@dataclass
class SavingsRow:
    line: int
    date: datetime
    description: str
    value_raw: str
    value: Decimal
    price: str
    quantity: str
    raw: dict[str, str]

    @property
    def ref(self) -> str:
        return source_hash(*(self.raw.get(key, "") for key in self.raw))


def _kind(description: str) -> str:
    for prefix in (
        "Service Fee Charged",
        "Return Reinvested",
        "Return WITHDRAWN",
        "Return PAID",
        "BUY",
        "SELL",
    ):
        if description.startswith(prefix):
            return prefix
    raise ValueError(f"Descripción de Revolut Savings desconocida: {description!r}")


def _trade_value(raw: str, *, external: bool) -> Decimal:
    value = parse_european_decimal(raw, "Value")
    stripped = raw.strip().lstrip("+-")
    # In Revolut's Spanish TSV, a dot is the thousands separator. Pure whole
    # numbers below 100 are also abbreviated as thousands for external trades
    # ("2" means 2,000), while interest markers keep their literal value.
    if external and "," not in stripped:
        if "." in stripped:
            decimals = stripped.split(".", 1)[1]
            if len(decimals) == 3:
                value = Decimal(stripped.replace(".", ""))
            else:
                value = Decimal(stripped) * 1000
            if raw.strip().startswith("-"):
                value = -value
        elif abs(value) < 100:
            value *= 1000
    return value


def _interest_value(raw: str) -> Decimal:
    # Revolut serializes daily amounts as four implied decimal places and may
    # omit both separators and leading zeroes. Thus 4,707 is 0.4707 and 212 is
    # 0.0212. Reading these as ordinary locale numbers corrupts long exports.
    stripped = raw.strip()
    sign = Decimal("-1") if stripped.startswith("-") else Decimal("1")
    digits = stripped.lstrip("+-").replace(".", "").replace(",", "")
    if not digits.isdigit():
        raise ValueError(f"daily return inválido: {raw!r}")
    return sign * Decimal(digits) / Decimal("10000")


def _resolve_external_trade_amounts(
    buys: list[SavingsRow],
    sells: list[SavingsRow],
    reinvested_by_buy: dict[int, SavingsRow],
    daily_net: dict[datetime, Decimal],
    overrides: dict[str, str],
) -> tuple[dict[int, Decimal], list[str]]:
    resolved: dict[int, Decimal] = {}
    warnings: list[str] = []
    position = Decimal("0")

    def nearby(date: datetime, before: bool) -> Decimal | None:
        values = [
            (abs(date - day), value)
            for day, value in daily_net.items()
            if value > 0
            and (
                timedelta(0) < date - day <= timedelta(days=7)
                if before
                else timedelta(0) < day - date <= timedelta(days=7)
            )
        ]
        return min(values, key=lambda item: item[0])[1] if values else None

    for row in sorted(buys + sells, key=lambda item: (item.date, item.line)):
        row_kind = _kind(row.description)
        direction = Decimal("1") if row_kind == "BUY" else Decimal("-1")
        marker = reinvested_by_buy.get(row.line)
        override_key = f"{iso_local(row.date)}|{row_kind}"
        if override_key in overrides:
            amount = abs(parse_european_decimal(str(overrides[override_key]), "amount override"))
            warnings.append(f"Línea {row.line}: importe forzado a {amount} por {override_key}")
        elif marker is not None:
            amount = abs(marker.value)
        else:
            literal = abs(parse_european_decimal(row.value_raw, "Value"))
            stripped = row.value_raw.strip().lstrip("+-")
            if "," in stripped or "." in stripped or literal >= 100:
                amount = abs(_trade_value(row.value_raw, external=True))
            else:
                candidates = [literal, literal * 1000]
                before = nearby(row.date, True)
                after = nearby(row.date, False)
                if position <= 0 or before is None or after is None or before == 0:
                    raise ValueError(
                        f"Línea {row.line}: importe entero ambiguo {row.value_raw!r}; "
                        "no hay rendimientos suficientes para determinar su escala"
                    )
                observed_ratio = after / before
                scored: list[tuple[Decimal, Decimal]] = []
                for candidate in candidates:
                    resulting_position = position + direction * candidate
                    if resulting_position < 0:
                        continue
                    predicted_ratio = resulting_position / position
                    scored.append((abs(predicted_ratio - observed_ratio), candidate))
                if not scored:
                    raise ValueError(f"Línea {row.line}: la operación dejaría participaciones negativas")
                scored.sort()
                amount = scored[0][1]
                if len(scored) > 1 and scored[1][0] - scored[0][0] < Decimal("0.02"):
                    raise ValueError(
                        f"Línea {row.line}: no se puede distinguir si {row.value_raw!r} "
                        f"representa {candidates[0]} o {candidates[1]}"
                    )
                warnings.append(
                    f"Línea {row.line}: {row.value_raw} interpretado como {amount} "
                    "según la variación del rendimiento diario"
                )
        resolved[row.line] = amount
        position += direction * amount
        if position < 0:
            raise ValueError(
                f"Línea {row.line}: la posición reconstruida es negativa "
                f"después de aplicar {direction * amount}; saldo={position}"
            )
    return resolved, warnings


def _find_match(
    marker: SavingsRow,
    candidates: list[SavingsRow],
    used: set[int],
    max_days: int,
) -> SavingsRow | None:
    amount = abs(marker.value)
    matches = [
        row
        for row in candidates
        if row.line not in used
        and abs(row.value) == amount
        and timedelta(0) <= row.date - marker.date <= timedelta(days=max_days)
    ]
    if not matches:
        return None
    matches.sort(key=lambda row: (row.date - marker.date, row.line))
    if len(matches) > 1 and matches[0].date - marker.date == matches[1].date - marker.date:
        raise ValueError(
            f"Línea {marker.line}: más de una operación coincide con {marker.description!r}"
        )
    return matches[0]


def _find_temporal_match(
    marker: SavingsRow,
    candidates: list[SavingsRow],
    used: set[int],
    max_days: int,
) -> SavingsRow | None:
    matches = [
        row
        for row in candidates
        if row.line not in used
        and timedelta(0) <= row.date - marker.date <= timedelta(days=max_days)
    ]
    if not matches:
        return None
    matches.sort(key=lambda row: (row.date - marker.date, row.line))
    return matches[0]


def parse_revolut_savings(path: Path, config: dict) -> ConversionResult:
    currency = config.get("currency", "EUR")
    external_transfers = bool(config.get("externalTransfers", False))
    symbol = config.get("symbol", f"REV-CASH-{currency}")
    isin = config.get("isin", "")
    value_column = f"Value, {currency}"
    expected_base = ["Date", "Description", value_column]

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not reader.fieldnames or reader.fieldnames[:3] != expected_base:
            raise ValueError(
                f"Cabecera de Revolut Savings {currency} no reconocida: {reader.fieldnames}"
            )
        source_rows = list(reader)

    rows: list[SavingsRow] = []
    for line_number, raw in enumerate(source_rows, 2):
        description = raw["Description"].strip()
        kind = _kind(description)
        value_raw = raw[value_column].strip()
        if kind in {"Return PAID", "Service Fee Charged"}:
            value = _interest_value(value_raw)
        else:
            value = _trade_value(value_raw, external=False)
        rows.append(
            SavingsRow(
                line=line_number,
                date=parse_revolut_local_date(raw["Date"]),
                description=description,
                value_raw=value_raw,
                value=value,
                price=raw.get("Price per share", "").strip(),
                quantity=raw.get("Quantity of shares", "").strip(),
                raw=raw,
            )
        )

    if not rows:
        raise ValueError("El TSV de Revolut Savings no contiene actividades")

    by_kind: dict[str, list[SavingsRow]] = defaultdict(list)
    for row in rows:
        by_kind[_kind(row.description)].append(row)

    activities: list[Activity] = []
    warnings: list[str] = []
    matched_sells: set[int] = set()
    withdrawn_by_sell: dict[int, SavingsRow] = {}
    for marker in by_kind["Return WITHDRAWN"]:
        match = _find_temporal_match(marker, by_kind["SELL"], matched_sells, 2)
        if match is None:
            raise ValueError(
                f"Línea {marker.line}: no se encontró SELL para el rendimiento retirado {marker.value_raw}"
            )
        matched_sells.add(match.line)
        withdrawn_by_sell[match.line] = marker

    matched_buys: set[int] = set()
    reinvested_by_buy: dict[int, SavingsRow] = {}
    for marker in by_kind["Return Reinvested"]:
        match = _find_match(marker, by_kind["BUY"], matched_buys, 7)
        if match is not None:
            matched_buys.add(match.line)
            reinvested_by_buy[match.line] = marker
            continue
        moved_to_withdrawal = any(
            abs(withdrawn.value) == abs(marker.value)
            and timedelta(0) <= withdrawn.date - marker.date <= timedelta(days=7)
            for withdrawn in by_kind["Return WITHDRAWN"]
        )
        if not moved_to_withdrawal:
            raise ValueError(
                f"Línea {marker.line}: no se encontró BUY para la reinversión {marker.value_raw}"
            )

    daily_net: dict[datetime, Decimal] = defaultdict(Decimal)
    for row in by_kind["Return PAID"] + by_kind["Service Fee Charged"]:
        daily_net[row.date] += row.value
    trade_amounts, scale_warnings = _resolve_external_trade_amounts(
        by_kind["BUY"],
        by_kind["SELL"],
        reinvested_by_buy,
        daily_net,
        config.get("amountOverrides", {}),
    )
    warnings.extend(scale_warnings)

    daily: dict[datetime, dict[str, SavingsRow]] = defaultdict(dict)
    for row in by_kind["Return PAID"] + by_kind["Service Fee Charged"]:
        kind = _kind(row.description)
        if kind in daily[row.date]:
            raise ValueError(f"Línea {row.line}: {kind} diario duplicado")
        daily[row.date][kind] = row
    for date, pair in daily.items():
        if set(pair) != {"Return PAID", "Service Fee Charged"}:
            raise ValueError(f"{iso_local(date)}: rendimiento o comisión diaria sin pareja")
        paid = pair["Return PAID"]
        fee = pair["Service Fee Charged"]
        amount = paid.value + fee.value
        ref = source_hash(paid.ref, fee.ref)
        activities.append(
            Activity(
                date=iso_local(date),
                activity_type="INTEREST",
                currency=currency,
                amount=amount,
                source_ref=f"revolut-savings-interest:{ref}",
                symbol=symbol,
                instrument_type="EQUITY",
                isin=isin,
                comment=f"Revolut {currency}: rendimiento neto diario [{ref[:16]}]",
            )
        )

    for row in by_kind["BUY"]:
        marker = reinvested_by_buy.get(row.line)
        amount = trade_amounts[row.line]
        ref = source_hash(row.ref, marker.ref if marker else "external")
        if marker is None:
            activities.append(
                Activity(
                    date=iso_local(row.date - timedelta(seconds=1)),
                    activity_type="TRANSFER_IN" if external_transfers else "DEPOSIT",
                    currency=currency,
                    amount=amount,
                    source_ref=f"revolut-savings-{'transfer-in' if external_transfers else 'deposit'}:{ref}",
                    comment=(
                        f"Revolut {currency}: transferencia interna al fondo [{ref[:16]}]"
                        if external_transfers
                        else f"Revolut {currency}: aportación externa [{ref[:16]}]"
                    ),
                )
            )
        activities.append(
            Activity(
                date=iso_local(row.date),
                activity_type="BUY",
                currency=currency,
                amount=amount,
                source_ref=f"revolut-savings-buy:{ref}",
                symbol=symbol,
                instrument_type="EQUITY",
                isin=isin,
                quantity=amount,
                unit_price=Decimal("1"),
                comment=(
                    f"Revolut {currency}: reinversión [{ref[:16]}]"
                    if marker
                    else f"Revolut {currency}: compra [{ref[:16]}]"
                ),
            )
        )

    for row in by_kind["SELL"]:
        amount = trade_amounts[row.line]
        marker = withdrawn_by_sell.get(row.line)
        withdrawn = amount + (abs(marker.value) if marker else Decimal("0"))
        ref = source_hash(row.ref, marker.ref if marker else "external")
        activities.append(
            Activity(
                date=iso_local(row.date),
                activity_type="SELL",
                currency=currency,
                amount=amount,
                source_ref=f"revolut-savings-sell:{ref}",
                symbol=symbol,
                instrument_type="EQUITY",
                isin=isin,
                quantity=amount,
                unit_price=Decimal("1"),
                comment=f"Revolut {currency}: venta [{ref[:16]}]",
            )
        )
        activities.append(
            Activity(
                date=iso_local(row.date + timedelta(seconds=1)),
                activity_type="TRANSFER_OUT" if external_transfers else "WITHDRAWAL",
                currency=currency,
                amount=withdrawn,
                source_ref=f"revolut-savings-{'transfer-out' if external_transfers else 'withdrawal'}:{ref}",
                comment=(
                    f"Revolut {currency}: transferencia interna desde el fondo [{ref[:16]}]"
                    if external_transfers
                    else f"Revolut {currency}: retirada externa [{ref[:16]}]"
                ),
            )
        )

    position = sum(
        (a.quantity or Decimal("0")) * (1 if a.activity_type == "BUY" else -1)
        for a in activities
        if a.activity_type in {"BUY", "SELL"}
    )
    cash = sum(
        a.amount
        * (
            1
            if a.activity_type in {"DEPOSIT", "TRANSFER_IN", "INTEREST", "SELL", "CREDIT", "DIVIDEND"}
            else -1
        )
        for a in activities
    )
    if position < 0:
        raise ValueError(f"La posición reconstruida es negativa: {position}")
    if cash < Decimal("-0.01"):
        raise ValueError(f"El efectivo reconstruido es negativo: {cash}")

    counts: dict[str, int] = defaultdict(int)
    for activity in activities:
        counts[activity.activity_type] += 1
    return ConversionResult(
        activities=activities,
        checks={
            "sourceRows": len(rows),
            "activityCounts": dict(counts),
            "endingQuantity": str(position),
            "endingCash": str(cash),
        },
        warnings=warnings,
    ).sorted()
