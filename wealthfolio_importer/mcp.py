from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
    def __init__(self, url: str, token: str, *, timeout: float = 60) -> None:
        self.url = url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout
        self.session_id: str | None = None
        self.request_id = 0
        if not self.token:
            raise McpError("El token MCP está vacío")

    @classmethod
    def from_environment(cls, url: str, token_file: Path, *, timeout: float = 60):
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as error:
            raise McpError(f"No existe el secreto MCP: {token_file}") from error
        return cls(url, token, timeout=timeout)

    @staticmethod
    def _parse_response(body: bytes) -> dict[str, Any]:
        text = body.decode("utf-8")
        for line in text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise McpError(f"Respuesta MCP no reconocida: {text[:300]}") from error

    def _post(self, payload: dict[str, Any], *, expect_json: bool = True) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            # Wealthfolio supports a regular JSON response. Request it
            # explicitly: advertising SSE makes rmcp keep the connection
            # available for server notifications after a completed tool call.
            "Accept": "application/json",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        request = Request(
            self.url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if not self.session_id:
                    self.session_id = response.headers.get("mcp-session-id")
                content_type = response.headers.get("Content-Type", "")
                if "text/event-stream" not in content_type:
                    body = response.read()
                else:
                    # Wealthfolio may keep an SSE response open after publishing
                    # the JSON-RPC result. Stop at this request's first result
                    # instead of waiting for the stream to close.
                    body = b""
                    for raw_line in response:
                        if not raw_line.startswith(b"data:"):
                            continue
                        candidate = raw_line[5:].strip()
                        if not candidate:
                            continue
                        try:
                            message = json.loads(candidate)
                        except json.JSONDecodeError:
                            continue
                        if message.get("id") == payload.get("id") and (
                            "result" in message or "error" in message
                        ):
                            body = candidate
                            break
                    if not body and expect_json:
                        raise McpError("El stream MCP terminó sin respuesta JSON-RPC")
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise McpError(f"Wealthfolio MCP respondió HTTP {error.code}: {detail[:500]}") from error
        except URLError as error:
            raise McpError(f"No se puede conectar con Wealthfolio MCP: {error.reason}") from error
        if not expect_json:
            return {}
        message = self._parse_response(body)
        if "error" in message:
            raise McpError(f"Error JSON-RPC de Wealthfolio: {message['error']}")
        return message

    def initialize(self) -> None:
        self.request_id += 1
        message = self._post(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "wealthfolio-importer", "version": "0.4.0"},
                },
            }
        )
        name = message.get("result", {}).get("serverInfo", {}).get("name")
        if name != "wealthfolio" or not self.session_id:
            raise McpError("La inicialización MCP no devolvió una sesión válida de Wealthfolio")
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_json=False,
        )

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.session_id:
            self.initialize()
        self.request_id += 1
        message = self._post(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        result = message.get("result", {})
        if result.get("isError"):
            content = result.get("content", [])
            text = "; ".join(item.get("text", "") for item in content if isinstance(item, dict))
            raise McpError(f"La herramienta MCP {name} falló: {text or result}")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = result.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    parsed = json.loads(item.get("text", ""))
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        raise McpError(f"La herramienta MCP {name} no devolvió contenido estructurado")

    def prepare(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return self.call("prepare_activity_import", {"activities": rows})

    def commit(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return self.call("commit_activity_import", {"activities": rows})

    def import_activities(
        self,
        activities: list[Activity],
        account_id: str,
        *,
        commit: bool,
        batch_size: int = 500,
    ) -> McpImportResult:
        if not account_id.strip():
            raise McpError("Falta wealthfolioAccountId en la cuenta")
        if batch_size < 1 or batch_size > 1000:
            raise McpError("MCP_IMPORT_BATCH_SIZE debe estar entre 1 y 1000")

        imported = skipped = duplicates = 0
        run_ids: list[str] = []
        for offset in range(0, len(activities), batch_size):
            chunk = activities[offset : offset + batch_size]
            rows = [
                activity_to_mcp(activity, account_id, offset + index + 1)
                for index, activity in enumerate(chunk)
            ]
            preview = self.prepare(rows)
            summary = preview.get("summary", {})
            invalid = int(summary.get("invalid", 0))
            duplicates += int(summary.get("duplicates", 0))
            if invalid:
                bad_rows = [row for row in preview.get("rows", []) if not row.get("isValid", False)]
                raise McpError(
                    f"Wealthfolio rechazó {invalid} actividad(es) en el preview: "
                    + json.dumps(bad_rows[:10], ensure_ascii=False)
                )
            if not commit:
                continue
            result = self.commit(rows)
            commit_summary = result.get("summary", {})
            imported += int(commit_summary.get("imported", 0))
            skipped += int(commit_summary.get("skipped", 0))
            failed = result.get("failed", [])
            if failed:
                raise McpError(
                    "Wealthfolio devolvió filas fallidas después del commit: "
                    + json.dumps(failed[:10], ensure_ascii=False)
                )
            run_id = result.get("importRunId")
            if run_id:
                run_ids.append(str(run_id))
        return McpImportResult(imported, skipped, duplicates, run_ids)
