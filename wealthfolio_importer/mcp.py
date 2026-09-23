from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .model import Activity, decimal_text


class McpError(RuntimeError):
    pass


def _mcp_date(value: str) -> str:
    """Keep source timestamps for local deduplication, but send valid MCP dates.

    Wealthfolio accepts a date-only value or an RFC3339 timestamp. Several bank
    exports provide a local timestamp with no offset; passing it through is
    rejected by commit_activity_import even when its preview succeeds.
    """
    parsed = datetime.fromisoformat(value)
    if "T" in value and parsed.tzinfo is None:
        # Revolut's Spanish statements use Europe/Madrid local time. Preserve
        # the sequence of same-day events, including the DST offset, while
        # making the value RFC3339-valid for Wealthfolio.
        return parsed.replace(tzinfo=ZoneInfo("Europe/Madrid")).isoformat()
    return value


def _json_number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def activity_to_mcp(activity: Activity, account_id: str, line_number: int) -> dict[str, Any]:
    if activity.tax:
        raise McpError(
            f"La actividad {activity.identifier[:12]} contiene tax={decimal_text(activity.tax)}, "
            "un campo que prepare_activity_import de Wealthfolio 3.8 no admite."
        )
    if activity.subtype:
        raise McpError(
            f"La actividad {activity.identifier[:12]} contiene subtype={activity.subtype}, "
            "un campo que prepare_activity_import de Wealthfolio 3.8 no admite."
        )

    row: dict[str, Any] = {
        "date": _mcp_date(activity.date),
        "activityType": activity.activity_type,
        "currency": activity.currency,
        "accountId": account_id,
        "lineNumber": line_number,
        "comment": activity.comment,
    }
    optional = {
        "symbol": activity.symbol or None,
        "quantity": _json_number(activity.quantity),
        "unitPrice": _json_number(activity.unit_price),
        "amount": _json_number(activity.amount),
        "fee": _json_number(activity.fee),
    }
    row.update({key: value for key, value in optional.items() if value is not None})
    return row


def mcp_field_warnings(activities: list[Activity]) -> list[str]:
    warnings: list[str] = []
    fx_count = sum(activity.fx_rate is not None for activity in activities)
    isin_count = sum(bool(activity.isin) for activity in activities)
    instrument_count = sum(bool(activity.instrument_type) for activity in activities)
    subtype_count = sum(bool(activity.subtype) for activity in activities)
    if fx_count:
        warnings.append(
            f"MCP 3.8 no acepta fxRate explícito en {fx_count} actividad(es); "
            "Wealthfolio resolverá la conversión histórica."
        )
    if isin_count:
        warnings.append(
            f"MCP 3.8 no acepta ISIN explícito en {isin_count} actividad(es); "
            "el activo se resolverá mediante el símbolo existente."
        )
    if instrument_count:
        warnings.append(
            f"MCP 3.8 no acepta instrumentType explícito en {instrument_count} actividad(es); "
            "Wealthfolio lo resolverá mediante el símbolo."
        )
    if subtype_count:
        warnings.append(
            f"MCP 3.8 no acepta subtype en {subtype_count} actividad(es); "
            "la importación automática se detendrá para no perder semántica."
        )
    return warnings


@dataclass
class McpImportResult:
    imported: int
    skipped: int
    duplicates: int
    import_run_ids: list[str]


class WealthfolioMcpClient:
    """Synchronous facade over the official asynchronous MCP HTTP transport.

    Wealthfolio uses the legacy, session-based Streamable HTTP variant. Its
    responses are SSE streams that stay open after a message, so a generic HTTP
    reader cannot reliably use EOF as a protocol boundary.
    """

    def __init__(self, url: str, token: str, *, timeout: float = 60) -> None:
        self.url = url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout
        if not self.token:
            raise McpError("El token MCP está vacío")

    @classmethod
    def from_environment(cls, url: str, token_file: Path, *, timeout: float = 60):
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as error:
            raise McpError(f"No existe el secreto MCP: {token_file}") from error
        return cls(url, token, timeout=timeout)

    async def _call_async(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as error:  # pragma: no cover - deployment dependency
            raise McpError("Falta la dependencia 'mcp' para la importación automática") from error
        try:
            async with streamablehttp_client(
                self.url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
                sse_read_timeout=self.timeout,
            ) as (read_stream, write_stream, _get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
        except Exception as error:
            raise McpError(f"Error al comunicarse con Wealthfolio MCP: {error}") from error
        if result.isError:
            text = "; ".join(
                getattr(item, "text", "") for item in result.content if getattr(item, "text", None)
            )
            raise McpError(f"La herramienta MCP {name} falló: {text or result}")
        structured = result.structuredContent
        if isinstance(structured, dict):
            return structured
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        raise McpError(f"La herramienta MCP {name} no devolvió contenido estructurado")

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        import asyncio
        return asyncio.run(self._call_async(name, arguments))

    def prepare(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return self.call("prepare_activity_import", {"activities": rows})

    def commit(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return self.call("commit_activity_import", {"activities": rows})

    def import_activities(self, activities: list[Activity], account_id: str, *, commit: bool, batch_size: int = 500) -> McpImportResult:
        if not account_id.strip():
            raise McpError("Falta wealthfolioAccountId en la cuenta")
        if batch_size < 1 or batch_size > 1000:
            raise McpError("MCP_IMPORT_BATCH_SIZE debe estar entre 1 y 1000")
        imported = skipped = duplicates = 0
        run_ids: list[str] = []
        for offset in range(0, len(activities), batch_size):
            chunk = activities[offset : offset + batch_size]
            rows = [activity_to_mcp(activity, account_id, offset + index + 1) for index, activity in enumerate(chunk)]
            preview = self.prepare(rows)
            summary = preview.get("summary", {})
            invalid = int(summary.get("invalid", 0))
            duplicates += int(summary.get("duplicates", 0))
            if invalid:
                bad_rows = [row for row in preview.get("rows", []) if not row.get("isValid", False)]
                raise McpError(f"Wealthfolio rechazó {invalid} actividad(es) en el preview: " + json.dumps(bad_rows[:10], ensure_ascii=False))
            if not commit:
                continue
            result = self.commit(rows)
            commit_summary = result.get("summary", {})
            imported += int(commit_summary.get("imported", 0))
            skipped += int(commit_summary.get("skipped", 0))
            failed = result.get("failed", [])
            if failed:
                raise McpError("Wealthfolio devolvió filas fallidas después del commit: " + json.dumps(failed[:10], ensure_ascii=False))
            run_id = result.get("importRunId")
            if run_id:
                run_ids.append(str(run_id))
        return McpImportResult(imported, skipped, duplicates, run_ids)
