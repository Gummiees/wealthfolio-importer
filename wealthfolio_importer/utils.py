from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


MONTHS = {
    "jan": 1,
    "ene": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "abr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "ago": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    "dic": 12,
}


def source_hash(*parts: object) -> str:
    raw = "\x1f".join("" if value is None else str(value) for value in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} inválido: {value!r}") from error


def parse_european_decimal(value: str, field: str) -> Decimal:
    normalized = value.strip().replace(" ", "")
    if not normalized:
        raise ValueError(f"{field} vacío")
    if "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    return parse_decimal(normalized, field)


def parse_revolut_local_date(value: str) -> datetime:
    match = re.fullmatch(
        r"(\d{1,2})\s+([A-Za-záéíóúñ]+)\s+(\d{4}),\s*(\d{1,2}):(\d{2}):(\d{2})",
        value.strip(),
        re.IGNORECASE,
    )
    if not match:
        raise ValueError(f"Fecha de Revolut no reconocida: {value!r}")
    day, month_name, year, hour, minute, second = match.groups()
    month = MONTHS.get(month_name.lower())
    if month is None:
        raise ValueError(f"Mes de Revolut no reconocido: {month_name!r}")
    return datetime(int(year), month, int(day), int(hour), int(minute), int(second))


def iso_local(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

