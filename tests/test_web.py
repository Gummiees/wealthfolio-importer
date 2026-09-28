from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from wealthfolio_importer.service import WatchService
from wealthfolio_importer.web.app import app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.inbox = self.root / "inbox"
        self.config.write_text(
            json.dumps(
                {
                    "accounts": {
                        "xtb/main": {"parser": "xtb", "currency": "EUR"},
                        "sabadell/principal": {"parser": "sabadell", "currency": "EUR"},
                    }
                }
            ),
            encoding="utf-8",
        )
        self.environ = {"CONFIG_PATH": os.environ.get("CONFIG_PATH"), "INBOX_DIR": os.environ.get("INBOX_DIR")}
        os.environ["CONFIG_PATH"] = str(self.config)
        os.environ["INBOX_DIR"] = str(self.inbox)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for key, value in self.environ.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_home_lists_configured_accounts_and_fonditel(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sabadell", response.text)
        self.assertIn("Fonditel", response.text)
        self.assertIn('"accept": ".xlsx"', response.text)

    def test_upload_queues_only_a_valid_extension(self):
        response = self.client.post(
            "/upload",
            data={"account_key": "xtb/main"},
            files={"statement": ("statement.xlsx", b"spreadsheet", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.inbox / "xtb" / "main" / "statement.xlsx").is_file())
        self.assertFalse(list(self.inbox.rglob("*.uploading")))

        invalid = self.client.post(
            "/upload",
            data={"account_key": "xtb/main"},
            files={"statement": ("statement.tsv", b"not a spreadsheet", "text/tab-separated-values")},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse((self.inbox / "xtb" / "main" / "statement.tsv").exists())

    def test_watcher_ignores_partial_web_uploads(self):
        temporary = self.inbox / "xtb" / "main" / "statement.xlsx.uploading"
        temporary.parent.mkdir(parents=True)
        temporary.write_bytes(b"partial")
        service = WatchService()
        self.assertEqual(service.files(), [])
