from __future__ import annotations

import json
import unittest
from unittest import mock

from wealthfolio_importer.mcp import WealthfolioMcpClient


class _Response:
    def __init__(self, payload: dict | None, headers: dict | None = None):
        self.payload = payload
        self.headers = headers or {}
        self._body = b"" if payload is None else json.dumps(payload).encode()
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int | None = None):
        if amount is None:
            amount = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk


class McpProtocolTests(unittest.TestCase):
    def test_initialize_and_call_tool(self):
        initialize = _Response(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"serverInfo": {"name": "wealthfolio", "version": "3.8.0"}},
            },
            {"mcp-session-id": "session-1"},
        )
        notification = _Response(None)
        tool_result = _Response(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [{"type": "text", "text": '{"summary":{"invalid":0}}'}],
                    "structuredContent": {"summary": {"invalid": 0}},
                    "isError": False,
                },
            }
        )
        with mock.patch(
            "wealthfolio_importer.mcp.urlopen",
            side_effect=[initialize, notification, tool_result],
        ) as opened:
            client = WealthfolioMcpClient("http://wealthfolio:8088/mcp", "wfp_test")
            result = client.prepare(
                [{"date": "2025-01-01", "activityType": "DEPOSIT", "currency": "EUR"}]
            )

        self.assertEqual(result["summary"]["invalid"], 0)
        self.assertEqual(opened.call_count, 3)
        tool_request = opened.call_args_list[-1].args[0]
        self.assertEqual(tool_request.headers["Mcp-session-id"], "session-1")
        body = json.loads(tool_request.data)
        self.assertEqual(body["method"], "tools/call")
        self.assertEqual(body["params"]["name"], "prepare_activity_import")


if __name__ == "__main__":
    unittest.main()
