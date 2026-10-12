import os, time, unittest
from unittest.mock import patch
import server

class BackendLogicTests(unittest.TestCase):
    def fixture(self):
        now = int(time.time())
        rows = [
            {"t": now-5*86400, "o": 90, "h": 103, "l": 89, "c": 100, "date": "2026-10-07"},
            {"t": now-4*86400, "o": 100, "h": 110, "l": 95, "c": 105, "date": "2026-10-08"},
            {"t": now-3*86400, "o": 105, "h": 112, "l": 103, "c": 108, "date": "2026-10-09"},
            {"t": now-2*86400, "o": 108, "h": 115, "l": 107, "c": 112, "date": "2026-10-10"},
            {"t": now-86400, "o": 112, "h": 120, "l": 111, "c": 120, "date": "2026-10-11"},
        ]
        return {"meta": {"regularMarketPrice": 112.5, "regularMarketTime": now-60}, "candles": rows, "symbol": "^NSEI", "range": "3mo", "interval": "1d"}

    def test_history_change_positive_signed_fields(self):
        chart = self.fixture()
        result = server.history_payload("^NSEI", "3mo", "1d", chart=chart, now=int(time.time()))
        self.assertTrue(result["ok"])
        self.assertEqual(result["difference"], 4.0)
        self.assertAlmostEqual(result["differencePct"], 4/108*100)
        self.assertEqual(result["change"], result["difference"])
        self.assertEqual(result["changePct"], result["differencePct"])
        self.assertEqual(result["candles"][-1]["c"], 120)

    def test_invalid_symbol_rejected(self):
        with self.assertRaises(ValueError):
            server.fetch_yahoo_chart("https://evil.example", "1d", "1d")

    def test_oi_not_faked_when_provider_missing(self):
        old_url = os.environ.pop("OI_PROVIDER_URL", None)
        try:
            with self.assertRaisesRegex(RuntimeError, "OI provider is not configured"):
                server.fetch_authorized_oi("NIFTY", "")
        finally:
            if old_url is not None:
                os.environ["OI_PROVIDER_URL"] = old_url


    def test_market_bundle_combines_oi_and_price_without_mixing_spot(self):
        oi = {"ok": True, "index": "NIFTY", "expiry": "2026-10-15", "spot": 25010.0,
              "call": 1000, "put": 1200, "callChange": 20, "putChange": 30,
              "source": "Authorized test feed", "fetchedAt": "test timestamp"}
        quote = {"ok": True, "price": 25005.0, "changePct": 0.25,
                 "source": "Test quote feed", "marketTime": "test quote time"}
        with patch.object(server, "fetch_authorized_oi", return_value=oi), patch.object(server, "quote_payload", return_value=quote):
            result = server.market_bundle("NIFTY", "current_week")
        self.assertTrue(result["ok"])
        self.assertEqual(result["price"], 25010.0)
        self.assertEqual(result["spot"], 25010.0)
        self.assertAlmostEqual(result["spotChangePct"], 0.25)
        self.assertIn("Test quote feed", result["source"])
        self.assertTrue(result["bundle"])

    def test_market_bundle_refuses_missing_authorized_oi(self):
        with patch.object(server, "fetch_authorized_oi", side_effect=RuntimeError("provider missing")):
            with self.assertRaisesRegex(RuntimeError, "provider missing"):
                server.market_bundle("NIFTY", "current_week")


    def test_dhan_expiry_selector_resolves_nearest_future(self):
        with patch.object(server, "datetime") as dt:
            dt.now.return_value.astimezone.return_value.date.return_value.isoformat.return_value = "2026-10-12"
            self.assertEqual(server._resolve_expiry("current_week", ["2026-10-15", "2026-10-22"]), "2026-10-15")

    def test_dhan_oi_aggregates_real_chain_fields(self):
        expiry_response = {"status": "success", "data": ["2026-10-15", "2026-10-22"]}
        chain_response = {"status": "success", "data": {"last_price": 25000, "oc": {
            "24900": {"ce": {"oi": 100, "previous_oi": 80}, "pe": {"oi": 200, "previous_oi": 190}},
            "25000": {"ce": {"oi": 300, "previous_oi": 250}, "pe": {"oi": 400, "previous_oi": 350}},
        }}}
        with patch.dict(os.environ, {"DHAN_CLIENT_ID": "test-client", "DHAN_ACCESS_TOKEN": "test-token", "DHAN_NIFTY_SECURITY_ID": "13", "DHAN_INDEX_SEGMENT": "IDX_I"}), \
             patch.object(server, "http_json_post", side_effect=[expiry_response, chain_response]):
            result = server.fetch_dhan_oi("NIFTY", "2026-10-15")
        self.assertEqual(result["spot"], 25000)
        self.assertEqual(result["call"], 400)
        self.assertEqual(result["put"], 600)
        self.assertEqual(result["callChange"], 70)
        self.assertEqual(result["putChange"], 60)
        self.assertEqual(result["provider"], "DhanHQ")

    def test_dhan_provider_requires_server_credentials(self):
        for key in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN"):
            os.environ.pop(key, None)
        with patch.dict(os.environ, {"OI_PROVIDER_MODE": "dhan"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN"):
                server.fetch_authorized_oi("NIFTY", "")

    def test_ten_minute_aggregation(self):
        now = int(time.time())
        result_obj = {"chart": {"result": [{"meta": {"regularMarketPrice": 12},
            "timestamp": [now-900, now-600, now-300, now],
            "indicators": {"quote": [{"open": [10, 11, 12, 13], "high": [12, 13, 14, 15], "low": [9, 10, 11, 12], "close": [11, 12, 13, 14]}]}}], "error": None}}
        with patch.object(server, "http_json", return_value=result_obj):
            result = server.fetch_yahoo_chart("TEST", "5d", "10m")
        self.assertEqual(result["interval"], "10m")
        self.assertEqual(len(result["candles"]), 2)
        self.assertEqual(result["candles"][0]["o"], 10)
        self.assertEqual(result["candles"][0]["h"], 13)
        self.assertEqual(result["candles"][0]["l"], 9)
        self.assertEqual(result["candles"][0]["c"], 12)

if __name__ == "__main__":
    unittest.main()
