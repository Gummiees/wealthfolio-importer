from __future__ import annotations

import csv
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..model import Activity, ConversionResult
from ..utils import parse_decimal, source_hash


_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
_COMPLETED = {"COMPLETADO", "COMPLETED"}
_EXCHANGE_TYPES = {"cambio", "exchange"}
_PAYMENT_TYPES = {
    "pago con tarjeta",
    "pago de revolut",
    "card payment",
    "cash withdrawal",
    "retirada de efectivo",
}
_TRANSFER_TYPES = {"transferir", "transfer", "bank transfer", "transferencia"}
_FEE_TYPES = {"comision", "fee"}
_CREDIT_TYPES = {"cashback", "reembolso", "refund"}
_DEFAULT_INTERNAL = re.compile(
    r"(?:\b(?:desde|a|from|to)\s+(?:eur|usd|gbp)\s+ahorro\b|\bconversi[oó]n\s+(?:a|to)\b)",
    re.IGNORECASE,
)


def _repair_mojibake(value: str) -> str:
    if not any(marker in value for marker in ("Ã", "Â", "â")):
        return value
    try:
        repaired = value.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    original_markers = value.count("Ã") + value.count("Â")
    repaired_markers = repaired.count("Ã") + repaired.count("Â")
    return repaired if repaired_markers < original_markers else value


def _normalized(value: str) -> str:
    import unicodedata

    value = _repair_mojibake(value).strip().lower()
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )


def _read_rows(path: Path) -> list[dict[str, str]]:
    data = path.read_bytes()
    text: str | None = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    if text is None:
        raise ValueError(f"No se pudo decodificar el TSV de Revolut: {path.name}")

    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    if reader.fieldnames is None:
        raise ValueError("El TSV de Revolut no contiene cabecera")
    reader.fieldnames = [_repair_mojibake(field).strip() for field in reader.fieldnames]
    rows = [
        {
            _repair_mojibake(str(key)).strip(): _repair_mojibake(value or "").strip()
            for key, value in row.items()
        }
        for row in reader
        if any((value or "").strip() for value in row.values())
    ]
    required = {
        "Tipo",
        "Producto",
        "Fecha de inicio",
        "Fecha de finalización",
        "Descripción",
        "Importe",
        "Comisión",
        "Divisa",
        "State",
        "Saldo",
    }
    missing = sorted(required - set(reader.fieldnames))
    if missing:
        raise ValueError("Faltan columnas en el TSV de Revolut: " + ", ".join(missing))
    return rows


def _date(value: str, line_number: int) -> datetime:
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            pass
    raise ValueError(f"Línea {line_number}: fecha de inicio no reconocida: {value!r}")


def _transfer_rule(description: str, config: dict) -> dict | None:
    for index, rule in enumerate(config.get("transferRules", []), 1):
        if not isinstance(rule, dict) or not rule.get("pattern"):
            raise ValueError(f"transferRules[{index}] debe incluir pattern")
        try:
            if re.search(str(rule["pattern"]), description, re.IGNORECASE):
                return rule
        except re.error as error:
            raise ValueError(f"transferRules[{index}] contiene una regex inválida") from error
    return {} if _DEFAULT_INTERNAL.search(description) else None


def _activity_type(kind: str, description: str, amount: Decimal, config: dict) -> str:
    normalized_kind = _normalized(kind)
    transfer = _transfer_rule(description, config)
    if normalized_kind in _EXCHANGE_TYPES or transfer is not None:
        return "TRANSFER_IN" if amount > 0 else "TRANSFER_OUT"
    if normalized_kind in _PAYMENT_TYPES:
        return "CREDIT" if amount > 0 else "WITHDRAWAL"
    if normalized_kind in _TRANSFER_TYPES:
        return "DEPOSIT" if amount > 0 else "WITHDRAWAL"
    if normalized_kind in _FEE_TYPES:
        return "CREDIT" if amount > 0 else "FEE"
    if normalized_kind in _CREDIT_TYPES and amount > 0:
        return "CREDIT"
    raise ValueError(f"Tipo de operación de Revolut no reconocido: {kind!r}")


def parse_revolut_current(path: Path, config: dict) -> ConversionResult:
    configured_currency = str(config.get("currency", "EUR")).upper()
    source_rows = _read_rows(path)
    if not source_rows:
        raise ValueError("El TSV de cuenta de Revolut no contiene movimientos")

    parsed: list[dict] = []
    skipped_statuses: dict[str, int] = {}
    for line_number, row in enumerate(source_rows, 2):
        status = row["State"].upper()
        if status not in _COMPLETED:
            status_key = status or "VACÍO"
            skipped_statuses[status_key] = skipped_statuses.get(status_key, 0) + 1
            continue
        amount = parse_decimal(row["Importe"], f"Línea {line_number}: importe")
        fee = parse_decimal(row["Comisión"], f"Línea {line_number}: comisión")
        balance = parse_decimal(row["Saldo"], f"Línea {line_number}: saldo")
        currency = row["Divisa"].upper()
        if currency != configured_currency:
            raise ValueError(
                f"Línea {line_number}: divisa {currency!r}; se esperaba {configured_currency!r}"
            )
        if amount == 0 and fee == 0:
            raise ValueError(f"Línea {line_number}: importe y comisión no pueden ser ambos cero")
        if fee < 0:
            raise ValueError(f"Línea {line_number}: comisión negativa no soportada: {fee}")
        parsed.append(
            {
                "line": line_number,
                "date": _date(row["Fecha de inicio"], line_number),
                "end_date": row["Fecha de finalización"],
                "kind": row["Tipo"],
                "product": row["Producto"],
                "description": row["Descripción"],
                "amount": amount,
                "fee": fee,
                "currency": currency,
                "balance": balance,
            }
        )
    if not parsed:
        raise ValueError("El TSV de cuenta de Revolut no contiene movimientos completados")

    opening = parsed[0]["balance"] - parsed[0]["amount"] + parsed[0]["fee"]
    previous_balance = opening
    for row in parsed:
        expected = previous_balance + row["amount"] - row["fee"]
        if expected != row["balance"]:
            raise ValueError(
                f"Línea {row['line']}: el saldo no reconcilia: {previous_balance} + "
                f"{row['amount']} - {row['fee']} = {expected}, no {row['balance']}"
            )
        previous_balance = row["balance"]

    activities: list[Activity] = []
    counts: dict[str, int] = {}
    account_key = str(config.get("wealthfolioAccountId") or path.parent.name)
    if config.get("includeOpeningBalance", True) and opening:
        opening_type = "DEPOSIT" if opening > 0 else "WITHDRAWAL"
        opening_date = str(config.get("openingDate", "")).strip()
        if opening_date:
            datetime.fromisoformat(opening_date)
        else:
            opening_date = (parsed[0]["date"] - timedelta(days=1)).date().isoformat()
        activities.append(
            Activity(
                date=opening_date,
                activity_type=opening_type,
                currency=configured_currency,
                amount=abs(opening),
                source_ref=f"revolut-current-opening:{account_key}",
                dedupe_key=f"revolut-current-opening:{account_key}",
                comment="Revolut cuenta: saldo inicial calculado desde el extracto",
            )
        )
        counts[opening_type] = 1

    for row in parsed:
        raw_ref = source_hash(
            row["date"].isoformat(),
            row["end_date"],
            row["kind"],
            row["product"],
            row["description"],
            row["amount"],
            row["fee"],
            row["currency"],
            row["balance"],
        )
        if row["amount"]:
            activity_type = _activity_type(
                row["kind"], row["description"], row["amount"], config
            )
            activities.append(
                Activity(
                    date=row["date"].date().isoformat(),
                    activity_type=activity_type,
                    currency=row["currency"],
                    amount=abs(row["amount"]),
                    source_ref=f"revolut-current:{raw_ref}:amount",
                    comment=(
                        f"Revolut: {row['description']} | tipo {row['kind']} | "
                        f"saldo {row['balance']}"
                    ),
                )
            )
            counts[activity_type] = counts.get(activity_type, 0) + 1
        if row["fee"]:
            activities.append(
                Activity(
                    date=row["date"].date().isoformat(),
                    activity_type="FEE",
                    currency=row["currency"],
                    amount=row["fee"],
                    source_ref=f"revolut-current:{raw_ref}:fee",
                    comment=(
                        f"Revolut: comisión de {row['description']} | "
                        f"saldo {row['balance']}"
                    ),
                )
            )
            counts["FEE"] = counts.get("FEE", 0) + 1

    warnings = []
    if skipped_statuses:
        warnings.append(
            "Movimientos no completados omitidos: "
            + ", ".join(
                f"{status}={count}" for status, count in sorted(skipped_statuses.items())
            )
        )
    return ConversionResult(
        activities=activities,
        checks={
            "sourceRows": len(source_rows),
            "completedRows": len(parsed),
            "skippedRows": sum(skipped_statuses.values()),
            "openingBalance": str(opening),
            "statementBalance": str(parsed[-1]["balance"]),
            "reconstructedBalance": str(previous_balance),
            "activityCounts": counts,
        },
        warnings=warnings,
    ).sorted()
