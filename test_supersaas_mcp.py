"""Run with: python3 -m unittest -v test_supersaas_mcp.py"""

import importlib.util
import json
import pathlib
import select
import subprocess
import sys
import unittest
from datetime import date, datetime, timezone


MODULE_PATH = pathlib.Path(__file__).with_name("supersaas_mcp.py")
spec = importlib.util.spec_from_file_location("supersaas_mcp", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
FIXTURES = pathlib.Path(__file__).with_name("fixtures")


class ScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schedule = module.parse_schedule(
            (FIXTURES / "SLEEP.html").read_text(),
            "https://www.supersaas.nl/schedule/downthehatch/SLEEP",
        )
        cls.data = json.loads((FIXTURES / "ajax-response.json").read_text())
        cls.now = datetime(2026, 9, 27, tzinfo=timezone.utc)

    def test_mondays_are_closed_by_weekday_mask(self):
        self.assertEqual(self.schedule.bit_prefs & 127, 0b1111001)
        self.assertEqual(module._periods(self.schedule, date(2026, 10, 19), self.data["exc"]), [])
        slots = module.calculate_slots(self.schedule, self.data,
                                       date(2026, 10, 19), date(2026, 10, 26), now=self.now)
        self.assertEqual(slots, [
            {"start": "2026-10-22 09:30", "end": "2026-10-22 12:30"}
        ])

    def test_tuesday_exception_opens_one_slot(self):
        slots = module.calculate_slots(self.schedule, self.data,
                                       date(2026, 10, 13), date(2026, 10, 14), now=self.now)
        self.assertEqual(slots, [
            {"start": "2026-10-13 09:30", "end": "2026-10-13 12:30"}
        ])

    def test_block_started_before_requested_week_closes_every_day(self):
        data = {"app": [], "exc": [[1802995200, 1803859170, 0]]}
        slots = module.calculate_slots(self.schedule, data,
                                       date(2027, 2, 22), date(2027, 3, 1), now=self.now)
        self.assertEqual(slots, [])

    def test_extra_opening_is_additive(self):
        day = date(2026, 10, 21)  # An already-open Wednesday.
        midnight = 1792540800
        periods = module._periods(self.schedule, day,
                                  [[midnight, midnight + 86370, 1, 60, 120]])
        self.assertIn((570, 1320), periods)
        self.assertIn((60, 120), periods)

    def test_url_matches_supplied_ajax_shape(self):
        url = module.ajax_url(self.schedule, date(2026, 10, 6), date(2026, 11, 3))
        self.assertIn("/ajax/resource/823084?v=12&token=1017954", url)
        self.assertIn("afrom=2026-10-06+00%3A00", url)
        self.assertIn("efrom=1970-01-01", url)
        self.assertIn("ed=r", url)

    def test_stdio_handshake_and_tool_list(self):
        process = subprocess.Popen(
            [sys.executable, str(MODULE_PATH)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        def exchange(message):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            ready, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, "MCP server did not respond within 10 seconds")
            return json.loads(process.stdout.readline())

        try:
            initialized = exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"}}})
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            process.stdin.flush()
            listed = exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
            self.assertEqual(initialized["result"]["serverInfo"]["version"], "0.1.0")
            self.assertEqual(listed["result"]["tools"][0]["name"], "find_available_slots")
        finally:
            process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
