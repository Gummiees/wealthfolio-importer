from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..model import Activity, ConversionResult
from ..utils import parse_european_decimal, source_hash


_DATE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_INTEREST = re.compile(r"(?:\bREMUN(?:\.|\b)|\bREMUNERACION\b|\bINTERESES?\b)", re.IGNORECASE)
_FEE = re.compile(r"\b(?:COMISI[ÓO]N|COMISIONES|CUOTA\s+DE\s+MANTENIMIENTO)\b", re.IGNORECASE)
_DEFAULT_TRANSFER = re.compile(r"^(?:TRASPASO\b|TARJETA\s+CREDITO\b)", re.IGNORECASE)


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError(f"No se pudo decodificar el TXT de Sabadell: {path.name}")


def _date(value: str, line_number: int, field: str) -> str:
    match = _DATE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Línea {line_number}: {field} inválida: {value!r}")
    day, month, year = match.groups()
    return f"{year}-{month}-{day}"


def _comment(
    concept: str,
    value_date: str,
    balance: Decimal,
    nif: str,
    reference: str,
) -> str:
    details = [
        f"Sabadell: {concept}",
        f"fecha valor {value_date}",
        f"saldo {balance}",
    ]
    if nif:
        details.append(f"NIF {nif}")
    if reference:
        details.append(f"ref {reference}")
    return " | ".join(details)


def _transfer_rule(concept: str, config: dict) -> dict | None:
    for index, rule in enumerate(config.get("transferRules", []), 1):
        if not isinstance(rule, dict) or not rule.get("pattern"):
            raise ValueError(f"transferRules[{index}] debe incluir pattern")
        try:
            matches = re.search(rule["pattern"], concept, re.IGNORECASE)
        except re.error as error:
            raise ValueError(f"transferRules[{index}] contiene una regex inválida") from error
        if matches:
            return rule
    if _DEFAULT_TRANSFER.search(concept):
        return {}
    return None


def _cash_type(amount: Decimal, concept: str, transfer: dict | None) -> str:
    if transfer is not None:
        return "TRANSFER_IN" if amount > 0 else "TRANSFER_OUT"
    if amount > 0 and _INTEREST.search(concept):
        return "INTEREST"
    if amount < 0 and _FEE.search(concept):
        return "FEE"
    return "DEPOSIT" if amount > 0 else "WITHDRAWAL"


def parse_sabadell(path: Path, config: dict) -> ConversionResult:
    currency = config.get("currency", "EUR").upper()
    lines = [line.strip() for line in _read_text(path).splitlines() if line.strip()]
    if not lines:
        raise ValueError("El TXT de Sabadell no contiene transacciones")

    parsed: list[dict] = []
    for line_number, line in enumerate(lines, 1):
        columns = line.split("|")
        if len(columns) != 7:
            raise ValueError(
                f"Línea {line_number}: se esperaban 7 columnas, encontradas {len(columns)}"
            )
        operation_raw, concept, value_raw, amount_raw, balance_raw, nif, reference = (
            value.strip() for value in columns
        )
        operation_date = _date(operation_raw, line_number, "fecha de operación")
        value_date = _date(value_raw, line_number, "fecha valor")
        amount = parse_european_decimal(amount_raw, f"Línea {line_number}: importe")
        balance = parse_european_decimal(balance_raw, f"Línea {line_number}: saldo")
        if amount == 0:
            raise ValueError(f"Línea {line_number}: el importe no puede ser cero")
        parsed.append(
            {
                "line": line_number,
                "operation_date": operation_date,
                "value_date": value_date,
                "amount": amount,
                "balance": balance,
                "concept": concept,
                "nif": nif,
                "reference": reference,
            }
        )

    activities: list[Activity] = []
    counts: dict[str, int] = {}
    opening_value: Decimal | None = None

    if config.get("includeOpeningBalance", True):
        oldest = min(
            enumerate(parsed),
            key=lambda item: (item[1]["operation_date"], -item[0]),
        )[1]
        opening = (
            parse_european_decimal(str(config["openingBalance"]), "openingBalance")
            if "openingBalance" in config
            else oldest["balance"] - oldest["amount"]
        )
        opening_value = opening
        opening_date = config.get("openingDate")
        if not opening_date:
            opening_date = (
                datetime.fromisoformat(oldest["operation_date"]) - timedelta(days=1)
            ).date().isoformat()
        else:
            datetime.fromisoformat(opening_date)
        if opening:
            opening_type = "DEPOSIT" if opening > 0 else "WITHDRAWAL"
            activities.append(
                Activity(
                    date=opening_date,
                    activity_type=opening_type,
                    currency=currency,
                    amount=abs(opening),
                    source_ref=f"sabadell-opening:{config.get('wealthfolioAccountId', path.parent.name)}",
                    dedupe_key=f"sabadell-opening:{config.get('wealthfolioAccountId', path.parent.name)}",
                    comment="Sabadell: saldo inicial calculado desde el extracto",
                )
            )
            counts[opening_type] = counts.get(opening_type, 0) + 1

    mirrored = 0
    for row in parsed:
        transfer = _transfer_rule(row["concept"], config)
        activity_type = _cash_type(row["amount"], row["concept"], transfer)
        raw_ref = source_hash(
            row["operation_date"],
            row["value_date"],
            row["amount"],
            row["balance"],
            row["concept"],
            row["nif"],
            row["reference"],
        )
        activity = Activity(
            date=row["operation_date"],
            activity_type=activity_type,
            currency=currency,
            amount=abs(row["amount"]),
            source_ref=f"sabadell:{raw_ref}:source",
            comment=_comment(
                row["concept"],
                row["value_date"],
                row["balance"],
                row["nif"],
                row["reference"],
            ),
        )
        activities.append(activity)
        counts[activity_type] = counts.get(activity_type, 0) + 1

        if transfer and transfer.get("mirror", False):
            target = str(transfer.get("targetAccount", "")).strip()
            if not target:
                raise ValueError(
                    f"Línea {row['line']}: una regla mirror requiere targetAccount"
                )
            mirror_type = "TRANSFER_OUT" if activity_type == "TRANSFER_IN" else "TRANSFER_IN"
            mirror = Activity(
                date=row["operation_date"],
                activity_type=mirror_type,
                currency=currency,
                amount=abs(row["amount"]),
                source_ref=f"sabadell:{raw_ref}:mirror:{target}",
                target_account=target,
                comment=(
                    f"Sabadell: contrapartida de {row['concept']} | "
                    f"cuenta origen {config.get('wealthfolioAccountId', 'Sabadell')}"
                ),
            )
            activities.append(mirror)
            counts[mirror_type] = counts.get(mirror_type, 0) + 1
            mirrored += 1

    newest = max(
        enumerate(parsed),
        key=lambda item: (item[1]["operation_date"], -item[0]),
    )[1]
    reconstructed_balance: Decimal | None = None
    if opening_value is not None:
        reconstructed_balance = opening_value + sum(
            (row["amount"] for row in parsed), Decimal("0")
        )
        if reconstructed_balance != newest["balance"]:
            raise ValueError(
                "El saldo reconstruido no coincide con el saldo final del extracto: "
                f"{reconstructed_balance} != {newest['balance']}"
            )
    return ConversionResult(
        activities=activities,
        checks={
            "sourceRows": len(parsed),
            "statementBalance": str(newest["balance"]),
            "reconstructedBalance": (
                str(reconstructed_balance) if reconstructed_balance is not None else "disabled"
            ),
            "openingBalance": str(
                next(
                    (
                        activity.amount if activity.activity_type == "DEPOSIT" else -activity.amount
                        for activity in activities
                        if activity.dedupe_key.startswith("sabadell-opening:")
                    ),
                    Decimal("0"),
                )
            ),
            "mirroredTransfers": mirrored,
            "activityCounts": counts,
        },
    ).sorted()


def _card_year(path: Path) -> str:
    match = re.search(r"(\d{2})(\d{2})(20\d{2})", path.name)
    if not match:
        raise ValueError(
            f"No se pudo determinar el año del fichero de tarjeta: {path.name}"
        )
    return match.group(3)


def parse_sabadell_card(path: Path, config: dict) -> ConversionResult:
    currency = config.get("currency", "EUR").upper()
    year = _card_year(path)
    activities: list[Activity] = []
    source_rows = 0
    counts: dict[str, int] = {}

    for line_number, line in enumerate(_read_text(path).splitlines(), 1):
        columns = [value.strip() for value in line.strip().split("|")]
        if len(columns) != 4 or not re.fullmatch(r"\d{2}/\d{2}", columns[0]):
            continue
        source_rows += 1
        date_raw, concept, locality, amount_raw = columns
        day, month = date_raw.split("/")
        date = f"{year}-{month}-{day}"
        datetime.fromisoformat(date)
        statement_amount = parse_european_decimal(
            re.sub(r"\s*EUR\s*$", "", amount_raw, flags=re.IGNORECASE),
            f"Línea {line_number}: importe",
        )
        if statement_amount == 0:
            raise ValueError(f"Línea {line_number}: el importe no puede ser cero")

        # En el extracto Sabadell, un importe positivo es una compra y uno
        # negativo es una devolución. La cuenta de crédito usa el signo de caja.
        cash_amount = -statement_amount
        activity_type = "WITHDRAWAL" if cash_amount < 0 else "CREDIT"
        ref = source_hash(date, concept, locality, statement_amount)
        activity = Activity(
            date=date,
            activity_type=activity_type,
            currency=currency,
            amount=abs(cash_amount),
            source_ref=f"sabadell-card:{ref}",
            comment=" | ".join(
                part for part in (f"Sabadell tarjeta: {concept}", locality) if part
            ),
        )
        activities.append(activity)
        counts[activity_type] = counts.get(activity_type, 0) + 1

    if not activities:
        raise ValueError("El TXT de tarjeta Sabadell no contiene transacciones")

    return ConversionResult(
        activities=activities,
        checks={"sourceRows": source_rows, "activityCounts": counts},
    ).sorted()
