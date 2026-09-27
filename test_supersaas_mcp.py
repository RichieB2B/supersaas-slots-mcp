"""Run with: python3 -m unittest -v test_supersaas_mcp.py"""

import dataclasses
import importlib.util
import json
import pathlib
import select
import subprocess
import sys
import unittest
from datetime import date, datetime, timezone
from unittest import mock

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


MODULE_PATH = pathlib.Path(__file__).with_name("supersaas_mcp.py")
VERSION = tomllib.loads(
    pathlib.Path(__file__).with_name("pyproject.toml").read_text()
)["project"]["version"]
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

    def test_zero_advance_limits_mean_unbounded(self):
        super_soon = datetime(2026, 10, 22, tzinfo=timezone.utc)
        day = (date(2026, 10, 22), date(2026, 10, 23))
        self.assertEqual(module.calculate_slots(self.schedule, self.data, *day, now=super_soon), [])
        no_minimum = dataclasses.replace(self.schedule, add_limit=0)
        self.assertEqual(module.calculate_slots(no_minimum, self.data, *day, now=super_soon),
                         [{"start": "2026-10-22 09:30", "end": "2026-10-22 12:30"}])
        no_maximum = dataclasses.replace(self.schedule, early_limit=0)
        self.assertEqual(module.calculate_slots(no_maximum, self.data,
                                                date(2026, 10, 19), date(2026, 10, 26),
                                                now=self.now),
                         [{"start": "2026-10-22 09:30", "end": "2026-10-22 12:30"}])

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
            self.assertEqual(initialized["result"]["serverInfo"]["version"], VERSION)
            self.assertEqual(listed["result"]["tools"][0]["name"], "find_available_slots")
        finally:
            process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()


class LinkedPageTests(unittest.TestCase):
    PAGE_URL = "https://www.down-the-hatch.nl/reserveren/"
    SCHEDULE_URL = "https://www.supersaas.nl/schedule/downthehatch/SLEEP"

    @classmethod
    def setUpClass(cls):
        cls.page = (FIXTURES / "RESERVEREN _ Down the hatch.html").read_bytes()

    def test_finds_the_single_schedule_link_on_a_business_page(self):
        self.assertEqual(module.find_schedule_links(self.page.decode()), (self.SCHEDULE_URL,))

    def test_resolve_follows_the_link_found_on_the_page(self):
        with mock.patch.object(module, "_get", return_value=(self.page, self.PAGE_URL)) as get:
            self.assertEqual(module.resolve_schedule_url(self.PAGE_URL),
                             (self.SCHEDULE_URL, self.PAGE_URL))
        get.assert_called_once_with(self.PAGE_URL)

    def test_resolve_uses_a_schedule_url_without_fetching_a_page(self):
        with mock.patch.object(module, "_get", side_effect=AssertionError("fetched")) as get:
            self.assertEqual(module.resolve_schedule_url(self.SCHEDULE_URL),
                             (self.SCHEDULE_URL, None))
        get.assert_not_called()

    def test_resolve_rejects_pages_without_ambiguous_or_missing_links(self):
        cases = {
            "<p>Bel ons</p>": "No link",
            f'<a href="{self.SCHEDULE_URL}">a</a>'
            f'<a href="https://www.supersaas.com/schedule/other/SURF">b</a>': "several",
        }
        for html, expected in cases.items():
            with self.subTest(html=html):
                with mock.patch.object(module, "_get", return_value=(html.encode(), self.PAGE_URL)):
                    with self.assertRaises(module.ScheduleError) as caught:
                        module.resolve_schedule_url(self.PAGE_URL)
                self.assertIn(expected, str(caught.exception))

    def test_resolve_normalises_protocol_relative_links(self):
        html = '<a href="//www.supersaas.nl/schedule/downthehatch/SLEEP">b</a>'
        with mock.patch.object(module, "_get", return_value=(html.encode(), self.PAGE_URL)):
            self.assertEqual(module.resolve_schedule_url(self.PAGE_URL)[0], self.SCHEDULE_URL)

    def test_entry_urls_must_be_plain_https(self):
        for url in ("http://www.down-the-hatch.nl/reserveren/",
                    "https://user:pass@www.down-the-hatch.nl/reserveren/",
                    "https://www.down-the-hatch.nl:8443/reserveren/",
                    "ftp://www.down-the-hatch.nl/reserveren/"):
            with self.subTest(url=url):
                with mock.patch.object(module, "_get", side_effect=AssertionError("fetched")):
                    with self.assertRaises(module.ScheduleError):
                        module.resolve_schedule_url(url)

    def test_links_pointing_off_domain_are_rejected(self):
        html = '<a href="https://evil.example/schedule/downthehatch/SLEEP">b</a>'
        with mock.patch.object(module, "_get", return_value=(html.encode(), self.PAGE_URL)):
            with self.assertRaises(module.ScheduleError):
                module.resolve_schedule_url(self.PAGE_URL)

    def test_fetch_failures_become_schedule_errors(self):
        with mock.patch.object(module, "urlopen", side_effect=OSError("403 Forbidden")):
            with self.assertRaises(module.ScheduleError) as caught:
                module.resolve_schedule_url(self.PAGE_URL)
        self.assertIn("Could not fetch", str(caught.exception))

    def test_load_schedule_reads_the_linked_page_as_a_schedule(self):
        with mock.patch.object(module, "_get",
                               return_value=((FIXTURES / "SLEEP.html").read_bytes(),
                                             self.SCHEDULE_URL)):
            schedule, final_url = module.load_schedule(self.SCHEDULE_URL)
        self.assertEqual(final_url, self.SCHEDULE_URL)
        self.assertEqual(schedule.rp_id, 823084)


class ConstraintParsingTests(unittest.TestCase):
    def test_positive_tokens_are_minutes_of_day(self):
        days = module.parse_constraints("570 810 1110", 1800)
        self.assertEqual(len(days), 7)
        self.assertTrue(all(day == (570, 810, 1110) for day in days))

    def test_zero_expands_to_the_hourly_grid(self):
        self.assertEqual(module.parse_constraints("0", 60)[0], tuple(range(0, 1440, 60)))

    def test_negative_tokens_expand_to_repeating_grids(self):
        self.assertEqual(module.parse_constraints(" -30 0", 600)[0], tuple(range(0, 1440, 30)))
        self.assertEqual(
            module.parse_constraints(" -55 -50 -45 -40 -35 -30 -25 -20 -15 -10 -5 0", 60)[0],
            tuple(range(0, 1440, 5)),
        )

    def test_grids_are_deduplicated_and_sorted(self):
        self.assertEqual(module.parse_constraints("810 570 810", 60)[0], (570, 810))
        self.assertEqual(module.parse_constraints("1440", 60)[0], (0,))
        self.assertEqual(module.parse_constraints("1440 0", 60)[0], tuple(range(0, 1440, 60)))

    def test_whitespace_runs_do_not_add_a_phantom_grid(self):
        self.assertEqual(module.parse_constraints("  540   720 ", 60)[0], (540, 720))

    def test_bitmask_overrides_replace_single_weekdays(self):
        days = module.parse_constraints("540 720:1=60 120", 60)
        self.assertEqual(days[0], (60, 120))  # Bit 0 is Sunday.
        self.assertEqual(days[1], (540, 720))
        days = module.parse_constraints("540:12=300", 60)
        self.assertEqual((days[0], days[1], days[2], days[3], days[4]),
                         ((540,), (540,), (300,), (300,), (540,)))

    def test_daily_rounding_keeps_one_start_time(self):
        self.assertEqual(module.parse_constraints("540 720 900", 86400)[0], (720,))
        self.assertEqual(module.parse_constraints("540", 86400)[0], (540,))
        self.assertEqual(len(set(module.parse_constraints("540 720:1=900", 86400))), 1)

    def test_malformed_constraints_raise(self):
        for text, rounding in [("", 60), ("   ", 60), ("abc", 60), ("-9999", 60),
                               ("540:1", 60), ("540:x=60", 60), ("540:256=60", 60)]:
            with self.subTest(text=text):
                with self.assertRaises(module.ScheduleError):
                    module.parse_constraints(text, rounding)


class MeetingRoomTests(unittest.TestCase):
    """A public schedule whose declared start time is the bare hourly grid."""

    URL = "https://www.supersaas.com/schedule/demo/Meeting_Rooms/Room_1"

    @classmethod
    def setUpClass(cls):
        cls.schedule = module.parse_schedule(
            (FIXTURES / "meeting-room.html").read_text(errors="replace"), cls.URL)
        cls.data = json.loads((FIXTURES / "meeting-room-ajax.json").read_text())
        cls.now = datetime(2026, 9, 27, tzinfo=timezone.utc)

    def test_grid_start_times_expand_before_slot_calculation(self):
        self.assertEqual(self.schedule.starts[0], tuple(range(0, 1440, 60)))
        self.assertEqual(self.schedule.duration_seconds, 3600)

    def test_opening_hours_filter_the_grid_to_bookable_hours(self):
        slots = module.calculate_slots(self.schedule, self.data,
                                       date(2026, 9, 28), date(2026, 9, 29), now=self.now)
        self.assertEqual(slots, [
            {"start": "2026-09-28 09:00", "end": "2026-09-28 10:00"},
            {"start": "2026-09-28 11:00", "end": "2026-09-28 12:00"},
            {"start": "2026-09-28 16:00", "end": "2026-09-28 17:00"},
        ])

    def test_week_reports_every_slot_free_at_midnight_would_not(self):
        slots = module.calculate_slots(self.schedule, self.data,
                                       date(2026, 9, 28), date(2026, 10, 3), now=self.now)
        self.assertEqual(len(slots), 23)
        self.assertEqual({slot["start"][11:] for slot in slots} & {"00:00"}, set())

    def test_buffer_and_bookings_still_apply_to_grid_starts(self):
        busy = {"app": [[1790586000, 1790589600, self.schedule.resource_id]], "exc": []}
        monday = module.calculate_slots(self.schedule, busy,
                                       date(2026, 9, 28), date(2026, 9, 29), now=self.now)
        self.assertNotIn({"start": "2026-09-28 09:00", "end": "2026-09-28 10:00"}, monday)
        self.assertEqual(len(monday), 7)
        buffered = dataclasses.replace(self.schedule, buffer_seconds=1800)
        self.assertEqual(len(module.calculate_slots(buffered, self.data,
                                                    date(2026, 9, 28), date(2026, 10, 3),
                                                    now=self.now)) < 23, True)


class DiagnosticsTests(unittest.TestCase):
    URL = "https://www.supersaas.com/schedule/demo/Meeting_Rooms"

    def test_schedule_without_published_availability_is_private(self):
        html = "<html><head><title>User Login</title></head><body></body></html>"
        with self.assertRaises(module.ScheduleError) as caught:
            module.parse_schedule(html, self.URL)
        self.assertIn("behind a login", str(caught.exception))

    def test_opening_hours_without_start_times_report_a_different_cause(self):
        html = "<script>var open_times = [540, 540, 540, 540, 540, 540, 540, 1080], rp_id = 1</script>"
        with self.assertRaises(module.ScheduleError) as caught:
            module.parse_schedule(html, self.URL)
        self.assertIn("no start times", str(caught.exception))

    def test_resource_picker_names_the_remedy(self):
        html = ("<script>var start=precalc_constraints('540 720'),"
                "open_times=[540,540,540,540,540,540,540,1080,1080,1080,1080,1080,1080,1080],"
                "cluster=0,complex=0,sync=false,filter=0,rp_id=9,token=9,bit_prefs=126,"
                "default_length=1800,buffer=0,add_limit=0,early_limit=0</script>")
        with self.assertRaises(module.ScheduleError) as caught:
            module.parse_schedule(html, self.URL)
        self.assertIn("pick a resource", str(caught.exception))

    def test_date_only_rental_is_named_rather_than_called_bad_duration(self):
        html = ("<script>var start=precalc_constraints('720 840'), rounding=86400,"
                "open_times=[0,0,0,0,0,0,0,1440,1440,1440,1440,1440,1440,1440],"
                "cluster=0,complex=0,sync=false,filter=405725,rp_id=9,token=9,bit_prefs=126,"
                "default_length=86400,buffer=0,add_limit=0,early_limit=0,"
                "resource[405725]={data:[0,0,\"House_1\",5]}</script>")
        with self.assertRaises(module.ScheduleError) as caught:
            module.parse_schedule(html, self.URL)
        self.assertIn("date-only", str(caught.exception))

    def test_a_24_hour_unit_would_return_no_slots_so_the_guard_is_load_bearing(self):
        schedule = module.parse_schedule(
            (FIXTURES / "meeting-room.html").read_text(errors="replace"), MeetingRoomTests.URL)
        schedule = dataclasses.replace(schedule, duration_seconds=86400)
        data = json.loads((FIXTURES / "meeting-room-ajax.json").read_text())
        self.assertEqual(module.calculate_slots(schedule, data,
                                                date(2026, 9, 28), date(2026, 10, 3),
                                                now=datetime(2026, 9, 27, tzinfo=timezone.utc)), [])


if __name__ == "__main__":
    unittest.main()
