"""Tests for LCD stream stats collection (no HTTP server / framebuffer)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import stream_lcd


class CollectStatsTests(unittest.TestCase):
    def test_pairing_and_jobs_from_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status = root / "status.json"
            status.write_text(
                json.dumps(
                    {
                        "pairing": "paired",
                        "cloud": "online",
                        "node_id": "n1",
                        "name": "pack-01",
                        "organization_name": "Acme",
                        "warehouse_name": "North",
                        "last_heartbeat_at": "2026-07-17T12:00:00+00:00",
                        "agent_version": "0.3.8",
                    }
                ),
                encoding="utf-8",
            )
            q = root / "queue"
            q.mkdir()
            (q / "job1.json").write_text("{}", encoding="utf-8")
            p = root / "processed"
            p.mkdir()
            (p / "old.json").write_text("{}", encoding="utf-8")
            (p / "older.json").write_text("{}", encoding="utf-8")

            with mock.patch("stream_lcd.collect_stats", wraps=stream_lcd.collect_stats):
                # Patch inventory to avoid CUPS
                with mock.patch("printers.inventory_payload", return_value=[
                    {
                        "cups_name": "Zebra_ZD",
                        "display_name": "Zebra ZD421",
                        "status": "idle",
                        "status_message": None,
                        "uri": "ipp://1.2.3.4/ipp/print",
                    }
                ]):
                    data = stream_lcd.collect_stats(
                        status_path=status,
                        queue_dir=q,
                        processed_dir=p,
                        credentials_path=root / "credentials.json",
                        config_dir=root / "etc",
                        state_dir=root,
                        api_base_url="https://example.test",
                        include_printers=True,
                    )

            self.assertEqual(data["pairing"]["pairing"], "paired")
            self.assertEqual(data["pairing"]["cloud"], "online")
            self.assertEqual(data["pairing"]["name"], "pack-01")
            self.assertEqual(data["pairing"]["organization_name"], "Acme")
            self.assertEqual(data["jobs"]["queued"], 1)
            self.assertEqual(data["jobs"]["processed"], 2)
            self.assertEqual(data["paths"]["credentials_present"], False)
            self.assertEqual(data["paths"]["api_base_url"], "https://example.test")
            self.assertEqual(len(data["printers"]), 1)
            self.assertEqual(data["printers"][0]["display_name"], "Zebra ZD421")
            self.assertIn("hostname", data["system"])
            self.assertIn("collected_at", data)

    def test_unpaired_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            data = stream_lcd.collect_stats(
                status_path=Path(td) / "missing.json",
                include_printers=False,
            )
        self.assertEqual(data["pairing"]["pairing"], "unpaired")
        self.assertEqual(data["jobs"]["queued"], 0)
        self.assertEqual(data["printers"], [])

    def test_stats_provider_cache(self):
        calls = {"n": 0}

        def coll():
            calls["n"] += 1
            return {"n": calls["n"], "pairing": {}, "system": {}, "jobs": {},
                    "printers": [], "update": {}, "paths": {}}

        sp = stream_lcd.StatsProvider(coll, cache_s=60.0)
        a = sp.get()
        b = sp.get()
        self.assertEqual(a["n"], 1)
        self.assertEqual(b["n"], 1)
        self.assertEqual(calls["n"], 1)

    def test_html_contains_top_layout_and_stats(self):
        # Sanity: page template is top-aligned and wires /api/stats
        self.assertIn("align-items: stretch", stream_lcd.HTML_PAGE)
        self.assertNotIn("justify-content: center", stream_lcd.HTML_PAGE)
        self.assertIn("/api/stats", stream_lcd.HTML_PAGE)
        self.assertIn("id=\"stats\"", stream_lcd.HTML_PAGE)


if __name__ == "__main__":
    unittest.main()
