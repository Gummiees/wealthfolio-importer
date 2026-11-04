from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

import xlrd
from openpyxl import load_workbook


def _value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value).strip()


def _text_rows(path: Path) -> list[list[str]]:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"No se pudo decodificar el archivo tabular: {path.name}")

    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters="\t,;")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    return [[value.strip() for value in row] for row in csv.reader(text.splitlines(), dialect)]


def _xlsx_rows(path: Path) -> list[list[str]]:
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            return [[_value(value) for value in row] for row in workbook.active.iter_rows(values_only=True)]
        finally:
            workbook.close()
    except Exception as error:
        raise ValueError(f"No se pudo abrir el Excel: {path.name}") from error


def _xls_rows(path: Path) -> list[list[str]]:
    try:
        workbook = xlrd.open_workbook(path)
        sheet = workbook.sheet_by_index(0)
    except xlrd.XLRDError as error:
        raise ValueError(f"No se pudo abrir el Excel: {path.name}") from error

    rows: list[list[str]] = []
    for row_number in range(sheet.nrows):
        row: list[str] = []
        for cell in sheet.row(row_number):
            if cell.ctype == xlrd.XL_CELL_DATE:
                row.append(xlrd.xldate_as_datetime(cell.value, workbook.datemode).isoformat(sep=" "))
            else:
                row.append(_value(cell.value))
        rows.append(row)
    return rows


def read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read a one-sheet text or Excel export while preserving its header row."""
    suffix = path.suffix.lower()
    if suffix in {".tsv", ".csv"}:
        raw_rows = _text_rows(path)
    elif suffix == ".xlsx":
        raw_rows = _xlsx_rows(path)
    elif suffix == ".xls":
        raw_rows = _xls_rows(path)
    else:
        raise ValueError(f"Formato tabular no soportado: {path.suffix}")
    if not raw_rows:
        raise ValueError(f"El archivo no contiene cabecera: {path.name}")

    header = raw_rows[0]
    rows = [row + [""] * (len(header) - len(row)) for row in raw_rows[1:] if any(row)]
    return header, rows
