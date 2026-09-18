from __future__ import annotations

import tempfile
import unittest
import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

from wealthfolio_importer.parsers import (
    parse_revolut_savings,
    parse_revolut_stocks,
    parse_xtb,
)
from wealthfolio_importer.mcp import McpError, McpImportResult, activity_to_mcp
from wealthfolio_importer.model import Activity
from wealthfolio_importer.service import WatchService


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_revolut_stocks_maps_cash_trade_and_tax_correction(self):
        path = self.write(
            "stocks.tsv",
            "Date\tTicker\tType\tQuantity\tPrice per share\tTotal Amount\tCurrency\tFX Rate\n"
            "2025-01-01T10:00:00Z\t\tCASH TOP-UP\t\t\tUSD 100\tUSD\t1.25\n"
            "2025-01-01T11:00:00Z\tACME\tBUY - MARKET\t2\tUSD 40\tUSD 80\tUSD\t1.25\n"
            "2025-02-01T11:00:00Z\tACME\tDIVIDEND TAX (CORRECTION)\t\t\tUSD -0.30\tUSD\t1.20\n"
            "2025-02-01T11:00:01Z\tACME\tDIVIDEND TAX (CORRECTION)\t\t\tUSD 0.30\tUSD\t1.20\n",
        )
        result = parse_revolut_stocks(path, {"currency": "USD"})
        self.assertEqual([a.activity_type for a in result.activities], ["DEPOSIT", "BUY", "TAX", "CREDIT"])
        self.assertEqual(result.activities[0].fx_rate, Decimal("0.8"))
        self.assertEqual(result.activities[-1].subtype, "REIMBURSEMENT")

    def test_revolut_savings_reconciles_interest_reinvestment_and_withdrawal(self):
        path = self.write(
            "savings.tsv",
            "Date\tDescription\tValue, EUR\tPrice per share\tQuantity of shares\n"
            "1 ene 2025, 12:00:00\tBUY EUR Class R TEST\t2.0\t1\t2.0\n"
            "2 ene 2025, 03:00:00\tReturn PAID EUR Class R TEST\t4,707\t\t\n"
            "2 ene 2025, 03:00:00\tService Fee Charged EUR Class TEST\t-1,107\t\t\n"
            "3 ene 2025, 01:00:00\tReturn Reinvested Class R EUR TEST\t-0,36\t\t\n"
            "3 ene 2025, 12:00:00\tBUY EUR Class R TEST\t0,36\t1\t0,36\n"
            "4 ene 2025, 03:00:00\tReturn PAID EUR Class R TEST\t1,300\t\t\n"
            "4 ene 2025, 03:00:00\tService Fee Charged EUR Class TEST\t-0,300\t\t\n"
            "4 ene 2025, 12:00:00\tReturn WITHDRAWN EUR Class R TEST\t-0,10\t\t\n"
            "4 ene 2025, 12:00:00\tSELL EUR Class R TEST\t-1.000\t1\t1.000\n",
        )
        result = parse_revolut_savings(
            path,
            {"currency": "EUR", "symbol": "REV-CASH-EUR", "isin": "TEST"},
        )
        self.assertEqual(
            result.checks["activityCounts"],
            {"DEPOSIT": 1, "BUY": 2, "INTEREST": 2, "SELL": 1, "WITHDRAWAL": 1},
        )
        self.assertEqual(Decimal(result.checks["endingQuantity"]), Decimal("1000.36"))
        self.assertEqual(Decimal(result.checks["endingCash"]), Decimal("0"))
        withdrawal = next(a for a in result.activities if a.activity_type == "WITHDRAWAL")
        self.assertEqual(withdrawal.amount, Decimal("1000.10"))

    def test_xtb_reconciles_cash_and_open_positions(self):
        path = self.root / "xtb.xlsx"
        workbook = Workbook()
        closed = workbook.active
        closed.title = "Closed Positions"
        for _ in range(4):
            closed.append([])
        closed.append(["Position ID", "Open Conversion Rate", "Close Conversion Rate"])

        cash = workbook.create_sheet("Cash Operations")
        for _ in range(4):
            cash.append([])
        cash.append([
            "Type", "Instrument", "Ticker", "Category", "Time", "Amount", "ID", "Comment", "Product", "Position ID"
        ])
        cash.append(["Deposit", "", "", "", datetime(2025, 1, 1), 100, "1", "deposit", "Investment Plans", ""])
        cash.append(["Stock purchase", "Example ETF", "TEST.DE", "ETF", datetime(2025, 1, 2), -80, "2", "OPEN BUY 2/2 @ 40", "Investment Plans", "20"])
        cash.append(["Free funds interest", "", "", "", datetime(2025, 1, 3), 1, "3", "interest", "Investment Plans", ""])
        cash.append(["Subaccount transfer", "", "", "", datetime(2025, 1, 3), 5, "4", "internal", "Investment Plans", ""])
        cash.append(["Subaccount transfer", "", "", "", datetime(2025, 1, 3), -5, "5", "internal", "My Trades", ""])
        cash.append(["Total", None, None, None, None, 21, None, None, None, None])

        opened = workbook.create_sheet("Open Positions")
        for _ in range(9):
            opened.append([])
        opened.append([
            "Product", "Instrument/Position", "Ticker", "Category", "Type", "Volume", "Value", "Current price", "Open price", "Open time (UTC)"
        ])
        opened.append(["Investment Plan", "Example ETF", "TEST.DE", "ETF", "", 2, 82, "", 40, ""])
        workbook.save(path)

        result = parse_xtb(path, {"currency": "EUR"})
        self.assertEqual([a.activity_type for a in result.activities], ["DEPOSIT", "BUY", "INTEREST"])
        self.assertEqual(result.checks["endingCash"], "21")
        self.assertEqual(result.checks["ignoredSubaccountTransfers"], 2)

    def test_watch_service_emits_only_new_activities(self):
        inbox = self.root / "inbox"
        account_dir = inbox / "revolut" / "stocks"
        account_dir.mkdir(parents=True)
        statement = (
            "Date\tTicker\tType\tQuantity\tPrice per share\tTotal Amount\tCurrency\tFX Rate\n"
            "2025-01-01T10:00:00Z\t\tCASH TOP-UP\t\t\tUSD 100\tUSD\t1.25\n"
        )
        config = self.root / "config.json"
        config.write_text(
            json.dumps({"accounts": {"revolut/stocks": {"parser": "revolut-stocks", "currency": "USD"}}}),
            encoding="utf-8",
        )
        environment = {
            "CONFIG_PATH": str(config),
            "INBOX_DIR": str(inbox),
            "OUTBOX_DIR": str(self.root / "outbox"),
            "PROCESSED_DIR": str(self.root / "processed"),
            "FAILED_DIR": str(self.root / "failed"),
            "STATE_DIR": str(self.root / "state"),
            "DRY_RUN": "false",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            (account_dir / "first.tsv").write_text(statement, encoding="utf-8")
            self.assertEqual(WatchService().run_once(), 0)
            (account_dir / "second.tsv").write_text(statement, encoding="utf-8")
            self.assertEqual(WatchService().run_once(), 0)
        outputs = list((self.root / "outbox").rglob("*.csv"))
        self.assertEqual(len(outputs), 1)

    def test_mcp_mapping_preserves_supported_fields_and_rejects_subtype(self):
        activity = Activity(
            date="2025-01-01T10:00:00Z",
            activity_type="BUY",
            currency="USD",
            amount=Decimal("100"),
            source_ref="source-1",
            symbol="AAPL",
            quantity=Decimal("0.5"),
            unit_price=Decimal("200"),
            fee=Decimal("1.25"),
            comment="broker reference",
        )
        row = activity_to_mcp(activity, "account-1", 7)
        self.assertEqual(row["accountId"], "account-1")
        self.assertEqual(row["lineNumber"], 7)
        self.assertEqual(row["quantity"], 0.5)
        self.assertEqual(row["fee"], 1.25)

        unsafe = Activity(
            date=activity.date,
            activity_type="CREDIT",
            currency="USD",
            amount=Decimal("1"),
            source_ref="source-2",
            subtype="REIMBURSEMENT",
        )
        with self.assertRaises(McpError):
            activity_to_mcp(unsafe, "account-1", 8)

    def test_watch_service_auto_imports_through_mcp_after_preview(self):
        inbox = self.root / "inbox"
        account_dir = inbox / "revolut" / "stocks"
        account_dir.mkdir(parents=True)
        statement = (
            "Date\tTicker\tType\tQuantity\tPrice per share\tTotal Amount\tCurrency\tFX Rate\n"
            "2025-01-01T10:00:00Z\t\tCASH TOP-UP\t\t\tUSD 100\tUSD\t1.25\n"
        )
        source = account_dir / "latest.tsv"
        source.write_text(statement, encoding="utf-8")
        config = self.root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "accounts": {
                        "revolut/stocks": {
                            "parser": "revolut-stocks",
                            "currency": "USD",
                            "wealthfolioAccountId": "account-1",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        environment = {
            "CONFIG_PATH": str(config),
            "INBOX_DIR": str(inbox),
            "OUTBOX_DIR": str(self.root / "outbox"),
            "PROCESSED_DIR": str(self.root / "processed"),
            "FAILED_DIR": str(self.root / "failed"),
            "STATE_DIR": str(self.root / "state"),
            "DRY_RUN": "false",
            "AUTO_IMPORT": "true",
        }
        client = mock.Mock()
        client.import_activities.return_value = McpImportResult(1, 0, 0, ["run-1"])
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch(
            "wealthfolio_importer.service.WealthfolioMcpClient.from_environment",
            return_value=client,
        ):
            self.assertEqual(WatchService().run_once(), 0)

        client.import_activities.assert_called_once()
        _, account_id = client.import_activities.call_args.args
        self.assertEqual(account_id, "account-1")
        self.assertTrue(client.import_activities.call_args.kwargs["commit"])
        self.assertFalse(list((self.root / "outbox").rglob("*.csv")))
        self.assertFalse(source.exists())
        self.assertTrue(list((self.root / "processed").rglob("latest.tsv")))


if __name__ == "__main__":
    unittest.main()
