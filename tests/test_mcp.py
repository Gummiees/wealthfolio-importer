from __future__ import annotations

import unittest
from unittest import mock

from wealthfolio_importer.mcp import WealthfolioMcpClient


class McpProtocolTests(unittest.TestCase):
    def test_call_tool_uses_official_streamable_transport(self):
        client = WealthfolioMcpClient("http://wealthfolio:8088/mcp", "wfp_test")

        class Result:
            isError = False
            structuredContent = {"summary": {"invalid": 0}}
            content = []

        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False
            async def initialize(self): return None
            async def call_tool(self, name, arguments):
                self.name, self.arguments = name, arguments
                return Result()

        class Transport:
            async def __aenter__(self): return (object(), object(), lambda: None)
            async def __aexit__(self, *_): return False

        session = Session()
        with (
            mock.patch("mcp.ClientSession", return_value=session),
            mock.patch("mcp.client.streamable_http.streamablehttp_client", return_value=Transport()) as transport,
        ):
            result = client.prepare([{"date": "2025-01-01", "activityType": "DEPOSIT", "currency": "EUR"}])
        self.assertEqual(result["summary"]["invalid"], 0)
        transport.assert_called_once()
        self.assertEqual(session.name, "prepare_activity_import")


if __name__ == "__main__":
    unittest.main()
