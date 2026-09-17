from __future__ import annotations

import argparse
import json
from pathlib import Path

from .converter import PARSERS, convert
from .model import write_csv
from .service import WatchService, print_preview


def _assignments(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Se esperaba ORIGEN=DESTINO: {value!r}")
        key, mapped = value.split("=", 1)
        result[key] = mapped
    return result


def _convert_config(args) -> dict:
    config = {"parser": args.parser, "currency": args.currency}
    if args.symbol:
        config["symbol"] = args.symbol
    if args.isin:
        config["isin"] = args.isin
    config["tickerAliases"] = _assignments(args.ticker_alias)
    config["tickerCurrencies"] = _assignments(args.ticker_currency)
    config["amountOverrides"] = _assignments(args.amount_override)
    return config


def _add_convert_arguments(parser, output_required: bool) -> None:
    parser.add_argument("--parser", required=True, choices=sorted(PARSERS))
    parser.add_argument("--input", required=True, type=Path)
    if output_required:
        parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--currency", default="EUR")
    parser.add_argument("--symbol")
    parser.add_argument("--isin")
    parser.add_argument("--ticker-alias", action="append", default=[])
    parser.add_argument("--ticker-currency", action="append", default=[])
    parser.add_argument(
        "--amount-override",
        action="append",
        default=[],
        metavar="TIMESTAMP|TYPE=AMOUNT",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convierte extractos a CSV de Wealthfolio")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preview = subparsers.add_parser("preview", help="Valida y muestra un resumen sin escribir")
    _add_convert_arguments(preview, False)
    convert_parser = subparsers.add_parser("convert", help="Genera un CSV completo")
    _add_convert_arguments(convert_parser, True)
    watch = subparsers.add_parser("watch", help="Procesa las cuentas configuradas del inbox")
    watch.add_argument("--once", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "watch":
        raise SystemExit(WatchService().run(once=args.once))
    try:
        config = _convert_config(args)
        result = convert(args.input, config)
        print_preview(result)
        if args.command == "convert":
            write_csv(args.output, result.activities)
            print(f"CSV generado: {args.output}")
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
