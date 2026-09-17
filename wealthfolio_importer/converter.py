from __future__ import annotations

from pathlib import Path
from datetime import datetime

from .model import ConversionResult
from .parsers import parse_revolut_savings, parse_revolut_stocks, parse_xtb


PARSERS = {
    "revolut-stocks": parse_revolut_stocks,
    "revolut-savings": parse_revolut_savings,
    "xtb": parse_xtb,
}

EXTENSIONS = {
    "revolut-stocks": {".tsv"},
    "revolut-savings": {".tsv"},
    "xtb": {".xlsx"},
}

ACTIVITY_TYPES = {
    "BUY",
    "SELL",
    "DEPOSIT",
    "WITHDRAWAL",
    "INTEREST",
    "DIVIDEND",
    "TAX",
    "FEE",
    "CREDIT",
}


def _validate(result: ConversionResult) -> None:
    identifiers: set[str] = set()
    for index, activity in enumerate(result.activities, 1):
        if activity.activity_type not in ACTIVITY_TYPES:
            raise ValueError(f"Actividad {index}: tipo no permitido {activity.activity_type!r}")
        if len(activity.currency) != 3 or activity.currency != activity.currency.upper():
            raise ValueError(f"Actividad {index}: divisa inválida {activity.currency!r}")
        if activity.amount < 0:
            raise ValueError(f"Actividad {index}: importe negativo")
        if activity.activity_type in {"BUY", "SELL"}:
            if not activity.symbol or activity.quantity is None or activity.quantity <= 0:
                raise ValueError(f"Actividad {index}: compraventa sin activo o cantidad positiva")
        try:
            datetime.fromisoformat(activity.date.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"Actividad {index}: fecha inválida {activity.date!r}") from error
        if activity.identifier in identifiers:
            raise ValueError(f"Actividad {index}: identificador determinista duplicado")
        identifiers.add(activity.identifier)


def convert(path: Path, config: dict) -> ConversionResult:
    parser_name = config.get("parser")
    parser = PARSERS.get(parser_name)
    if parser is None:
        raise ValueError(f"Parser desconocido: {parser_name!r}")
    if path.suffix.lower() not in EXTENSIONS[parser_name]:
        expected = ", ".join(sorted(EXTENSIONS[parser_name]))
        raise ValueError(f"{parser_name} requiere {expected}; recibido: {path.suffix}")
    result = parser(path, config)
    _validate(result)
    return result
