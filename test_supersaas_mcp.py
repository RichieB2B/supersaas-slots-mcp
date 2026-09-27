"""Run with: python3 -m unittest -v test_supersaas_mcp.py"""

import dataclasses
import importlib.util
import json
import pathlib
import select
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
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

    def test_hourly_rounding_does_not_stop_a_24_hour_unit_being_date_only(self):
        html = ("<script>var start=precalc_constraints('900'), rounding=60,"
                "open_times=[0,0,0,0,0,0,0,1440,1440,1440,1440,1440,1440,1440],"
                "cluster=0,complex=0,sync=false,filter=1225905,rp_id=9,token=9,bit_prefs=126,"
                "default_length=86400,buffer=0,add_limit=0,early_limit=0,"
                "resource[1225905]={data:[0,0,\"Lot_101-40ft\",0]}</script>")
        schedule = module.parse_schedule(html, self.URL)
        self.assertTrue(schedule.date_only)
        self.assertEqual((schedule.checkin_minute, schedule.checkout_minute), (900, 900))

    def test_multi_day_units_are_rejected_rather_than_modelled_as_one_night(self):
        html = ("<script>var start=precalc_constraints('900'), rounding=60,"
                "open_times=[0,0,0,0,0,0,0,1440,1440,1440,1440,1440,1440,1440],"
                "cluster=0,complex=0,sync=false,filter=1225905,rp_id=9,token=9,bit_prefs=126,"
                "default_length=172800,buffer=0,add_limit=0,early_limit=0,"
                "resource[1225905]={data:[0,0,\"Lot_101-40ft\",0]}</script>")
        with self.assertRaises(module.ScheduleError) as caught:
            module.parse_schedule(html, self.URL)
        self.assertIn("units of 2 days", str(caught.exception))

    def test_a_24_hour_unit_would_return_no_slots_so_the_guard_is_load_bearing(self):
        schedule = module.parse_schedule(
            (FIXTURES / "meeting-room.html").read_text(errors="replace"), MeetingRoomTests.URL)
        schedule = dataclasses.replace(schedule, duration_seconds=86400)
        data = json.loads((FIXTURES / "meeting-room-ajax.json").read_text())
        self.assertEqual(module.calculate_slots(schedule, data,
                                                date(2026, 9, 28), date(2026, 10, 3),
                                                now=datetime(2026, 9, 27, tzinfo=timezone.utc)), [])


class RentalHomeTests(unittest.TestCase):
    """A public date-only schedule: nightly rental, check-in 14:00, check-out 12:00."""

    URL = "https://www.supersaas.com/schedule/demo/Rental_Homes/House_1"

    @classmethod
    def setUpClass(cls):
        cls.schedule = module.parse_schedule(
            (FIXTURES / "rental-home.html").read_text(errors="replace"), cls.URL)
        cls.data = json.loads((FIXTURES / "rental-home-ajax.json").read_text())
        cls.now = datetime(2026, 9, 27, tzinfo=timezone.utc)

    def nights(self, first, last):
        return module.calculate_slots(self.schedule, self.data, first, last, now=self.now)

    def test_page_is_recognised_as_date_only_with_check_in_and_out(self):
        self.assertTrue(self.schedule.date_only)
        self.assertEqual(self.schedule.checkin_minute, 840)
        self.assertEqual(self.schedule.checkout_minute, 720)
        self.assertEqual(self.schedule.duration_seconds, 86400)
        self.assertEqual(self.schedule.starts, ((840,),) * 7)

    def test_night_runs_from_check_in_to_next_morning_check_out(self):
        night = self.nights(date(2026, 10, 5), date(2026, 10, 6))[0]
        self.assertEqual(night, {"start": "2026-10-05 14:00", "end": "2026-10-06 12:00"})

    def test_sunday_check_ins_are_closed_by_bit_prefs(self):
        nights = self.nights(date(2026, 9, 28), date(2026, 11, 30))
        self.assertTrue(nights)
        for night in nights:
            self.assertNotEqual(date.fromisoformat(night["start"][:10]).weekday(), 6)

    def test_booked_night_is_dropped_but_its_check_out_night_survives(self):
        nights = {n["start"][:10] for n in self.nights(date(2026, 11, 15), date(2026, 11, 20))}
        self.assertNotIn("2026-11-16", nights)  # Occupied by the saved appointment.
        self.assertIn("2026-11-17", nights)     # Check-out day is bookable again.
        self.assertIn("2026-11-18", nights)

    def test_other_resources_do_not_block_this_one(self):
        nights = self.nights(date(2026, 9, 28), date(2026, 9, 29))
        self.assertIn({"start": "2026-09-28 14:00", "end": "2026-09-29 12:00"}, nights)

    def test_consecutive_nights_merge_into_stays(self):
        nights = self.nights(date(2026, 9, 28), date(2026, 11, 30))
        stays = module.merge_stays(nights)
        self.assertEqual(sum(stay["nights"] for stay in stays), len(nights))
        previous_end = None
        for stay in stays:
            span = (date.fromisoformat(stay["end"]) - date.fromisoformat(stay["start"])).days
            self.assertEqual(stay["nights"], span)
            self.assertGreaterEqual(stay["nights"], 1)
            if previous_end is not None:
                self.assertGreaterEqual(stay["start"], previous_end)  # Disjoint and ordered.
            previous_end = stay["end"]
            for offset in range(stay["nights"]):
                check_in = date.fromisoformat(stay["start"]) + timedelta(days=offset)
                self.assertNotEqual(check_in.weekday(), 6)  # No night starts on Sunday.
        self.assertEqual(stays[0], {"start": "2026-09-28", "end": "2026-10-03", "nights": 5})

    def test_blocked_range_exception_closes_nights(self):
        # Nov 12 00:00 to Nov 17 00:00 UTC overlaps check-in dates 12, 13, 14 and 16.
        blocked = {"app": [], "exc": [[1794441600, 1794873600, 0]]}
        nights = {n["start"][:10] for n in module.calculate_slots(
            self.schedule, blocked, date(2026, 11, 9), date(2026, 11, 21), now=self.now)}
        self.assertFalse(nights & {"2026-11-12", "2026-11-13", "2026-11-14", "2026-11-16"})
        self.assertTrue({"2026-11-09", "2026-11-10", "2026-11-17", "2026-11-18"} <= nights)
        self.assertNotIn("2026-11-15", nights)  # Sunday, closed by bit_prefs regardless.

    def test_booking_window_applies_to_the_check_in_instant(self):
        # The live demo sets no limits, so impose a one-day minimum advance.
        limited = dataclasses.replace(self.schedule, add_limit=86400)
        nights = module.calculate_slots(limited, self.data,
                                       date(2026, 10, 1), date(2026, 10, 8),
                                       now=datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.assertTrue(nights)
        for night in nights:
            self.assertGreaterEqual(night["start"], "2026-10-07 14:00")


class NightTimeTests(unittest.TestCase):
    def test_two_constraints_map_later_check_in_and_earlier_check_out(self):
        self.assertEqual(module._night_times("720 840"), (840, 720))
        self.assertEqual(module._night_times("540 1080"), (1080, 540))

    def test_single_constraint_makes_a_full_24_hour_night(self):
        self.assertEqual(module._night_times("720"), (720, 720))
        self.assertEqual(module._night_times("840"), (840, 840))

    def test_repeat_grids_cannot_be_read_as_night_times(self):
        for text, count in [("0", "24"), (" -30 0", "48"), ("570 810 1110", "3")]:
            with self.subTest(text=text):
                with self.assertRaises(module.ScheduleError) as caught:
                    module._night_times(text)
                self.assertIn(f"from {count} start times", str(caught.exception))

    def test_a_full_24_hour_night_spans_exactly_one_day(self):
        schedule = module.parse_schedule(
            (FIXTURES / "rental-home.html").read_text(errors="replace"), RentalHomeTests.URL)
        schedule = dataclasses.replace(schedule, starts=((720,),) * 7,
                                       checkin_minute=720, checkout_minute=720)
        night = module.calculate_slots(schedule, {"app": [], "exc": []},
                                       date(2026, 10, 5), date(2026, 10, 6),
                                       now=datetime(2026, 9, 27, tzinfo=timezone.utc))[0]
        self.assertEqual(night, {"start": "2026-10-05 12:00", "end": "2026-10-06 12:00"})


class ScriptDiscoveryTests(unittest.TestCase):
    PAGE_URL = "https://www.example-marina.test/booking"
    SCHEDULE_URL = "https://www.supersaas.com/schedule/GuistCreekMarina/Pull_Through_RV_Sites"
    BUNDLE = "https://www.example-marina.test/assets/index-CPjZSA-J.js"

    def page(self, *sources):
        return "".join(f'<script src="{s}"></script>' for s in sources)

    def test_only_same_origin_script_sources_are_collected(self):
        html = self.page("/assets/index-CPjZSA-J.js", self.BUNDLE,
                         "https://cdn.supersaas.net/widget.js", "//other.test/x.js")
        self.assertEqual(module.same_origin_scripts(html, self.PAGE_URL), (self.BUNDLE,))

    def test_relative_and_duplicate_sources_resolve_once(self):
        html = self.page("/assets/index-CPjZSA-J.js", "/assets/index-CPjZSA-J.js", self.BUNDLE)
        self.assertEqual(module.same_origin_scripts(html, self.PAGE_URL), (self.BUNDLE,))

    def test_inline_scripts_without_src_are_ignored(self):
        self.assertEqual(module.same_origin_scripts("<script>var x=1</script>", self.PAGE_URL), ())

    def test_scanning_stops_at_the_script_cap(self):
        html = self.page(*[f"/assets/chunk{i}.js" for i in range(30)])
        self.assertEqual(len(module.same_origin_scripts(html, self.PAGE_URL)), module.MAX_SCRIPTS)

    def test_a_schedule_link_inside_a_bundle_is_found(self):
        bundle = f'const k2="{self.SCHEDULE_URL}";new window.SuperSaaS("633382:GuistCreekMarina")'
        self.assertEqual(module.find_schedule_links(bundle), (self.SCHEDULE_URL,))

    def test_resolve_falls_back_to_the_bundle(self):
        html = self.page("/assets/index-CPjZSA-J.js")
        bundle = f'const u="{self.SCHEDULE_URL}";'
        with mock.patch.object(module, "_get", side_effect=[
                (html.encode(), self.PAGE_URL), (bundle.encode(), self.BUNDLE)]) as get:
            self.assertEqual(module.resolve_schedule_url(self.PAGE_URL),
                             (self.SCHEDULE_URL, self.PAGE_URL))
        self.assertEqual([c.args[0] for c in get.call_args_list], [self.PAGE_URL, self.BUNDLE])

    def test_an_html_link_is_used_without_fetching_scripts(self):
        html = f'<a href="{self.SCHEDULE_URL}">book</a>' + self.page("/assets/a.js")
        with mock.patch.object(module, "_get", return_value=(html.encode(), self.PAGE_URL)) as get:
            self.assertEqual(module.resolve_schedule_url(self.PAGE_URL),
                             (self.SCHEDULE_URL, self.PAGE_URL))
        get.assert_called_once_with(self.PAGE_URL)

    def test_an_unreachable_bundle_does_not_hide_a_later_link(self):
        html = self.page("/assets/gone.js", "/assets/live.js")
        with mock.patch.object(module, "_get", side_effect=[
                (html.encode(), self.PAGE_URL), module.ScheduleError("404"),
                (f'x="{self.SCHEDULE_URL}"'.encode(), "https://x")]) as get:
            self.assertEqual(module.resolve_schedule_url(self.PAGE_URL),
                             (self.SCHEDULE_URL, self.PAGE_URL))
        self.assertEqual(get.call_count, 3)

    def test_no_link_anywhere_still_raises(self):
        html = self.page("/assets/a.js")
        with mock.patch.object(module, "_get", side_effect=[
                (html.encode(), self.PAGE_URL), (b"const x=1;", self.BUNDLE)]):
            with self.assertRaises(module.ScheduleError) as caught:
                module.resolve_schedule_url(self.PAGE_URL)
        self.assertIn("No link", str(caught.exception))


class ResponseShapeTests(unittest.TestCase):
    def test_night_mode_adds_unit_clock_times_and_stays(self):
        page = (FIXTURES / "rental-home.html").read_bytes()
        ajax = (FIXTURES / "rental-home-ajax.json").read_bytes()
        with mock.patch.object(module, "_get",
                               side_effect=[(page, RentalHomeTests.URL), (ajax, "https://x")]):
            result = module.find_available_slots(RentalHomeTests.URL, "2026-09-28", "2026-10-25")
        self.assertEqual(result["unit"], "night")
        self.assertEqual(result["check_in"], "14:00")
        self.assertEqual(result["check_out"], "12:00")
        self.assertEqual(result["duration_minutes"], 1440)
        self.assertEqual(result["count"], len(result["slots"]))
        self.assertEqual(result["stays"][0],
                         {"start": "2026-09-28", "end": "2026-10-03", "nights": 5})
        self.assertEqual(sum(stay["nights"] for stay in result["stays"]), result["count"])

    def test_slot_mode_keeps_its_original_shape(self):
        page = (FIXTURES / "meeting-room.html").read_bytes()
        ajax = (FIXTURES / "meeting-room-ajax.json").read_bytes()
        with mock.patch.object(module, "_get",
                               side_effect=[(page, MeetingRoomTests.URL), (ajax, "https://x")]):
            result = module.find_available_slots(MeetingRoomTests.URL, "2026-09-28", "2026-10-02")
        self.assertEqual(result["unit"], "slot")
        self.assertEqual(result["duration_minutes"], 60)
        self.assertEqual(result["count"], 23)
        for key in ("stays", "check_in", "check_out"):
            self.assertNotIn(key, result)


class CapacityScheduleTests(unittest.TestCase):
    URL = "https://www.supersaas.nl/schedule/Jazzercise"
    PAGE = (b'<script src="/assets/capacity-82a8c6eb1aa8b01938164e40c3a9dcb1684782bf5ea65e0c0b9752b635bfced4.js"></script>'
            b'<script>var rp_id=508523,token=866123,overbooking=3,app=[],add_limit=0,sync=false,first_hour=9,'
            b'from_utc=[1806195600,3600,1792890000,0,1774746000,3600,1761440400,0]</script>')

    @staticmethod
    def row(day, hour, slot_id, capacity, booked, title="Jazzercise", location="Village Hall",
            waiting=0):
        begin = int(datetime(2026, 10, day, hour, tzinfo=timezone.utc).timestamp())
        return [begin, begin + 3600, slot_id, capacity, booked, 3, 14,
                title, "", waiting, location, 0]

    def test_capacity_page_and_ajax_url(self):
        schedule = module.parse_schedule(self.PAGE.decode(), self.URL)
        self.assertIsInstance(schedule, module.CapacitySchedule)
        self.assertEqual(schedule.rp_id, 508523)
        self.assertEqual(schedule.utc_offsets[2], (1774746000, 3600))
        url = module.ajax_url(schedule, date(2026, 10, 1), date(2026, 10, 8))
        self.assertIn("/ajax/capacity/508523?v=12&token=866123", url)
        self.assertIn("afrom=2026-10-01&ato=2026-10-08", url)

    def test_saved_jazzercise_ajax_response(self):
        schedule = module.parse_schedule(self.PAGE.decode(), self.URL)
        data = json.loads((FIXTURES / "jazzercise-capacity-ajax.json").read_text())
        slots = module.calculate_capacity_slots(
            schedule, data, date(2026, 9, 27), date(2026, 10, 5),
            now=datetime(2026, 9, 27, tzinfo=timezone.utc))
        self.assertEqual(len(slots), 13)
        self.assertEqual(slots[0]["id"], 73524942)
        self.assertEqual(slots[0]["start"], "2026-09-28 19:00")
        self.assertEqual(slots[0]["available"], 8)
        self.assertEqual(slots[0]["waiting"], 0)
        self.assertEqual(slots[-1]["capacity"], 20)
        self.assertEqual(slots[-1]["available"], 7)

    def test_remaining_seats_and_class_metadata(self):
        schedule = module.parse_schedule(self.PAGE.decode(), self.URL)
        rows = [
            self.row(1, 19, 101, 40, 32, "CardioSculpt", "Black Notley Village Hall"),
            self.row(1, 20, 102, 40, 40),
            self.row(2, 9, 103, -1, 8),
            self.row(2, 10, 104, 0, 0),
            self.row(2, 11, 105, 20, -2),
            self.row(2, 12, 107, 40, 40, waiting=2),
            self.row(8, 9, 106, 40, 0),
        ]
        slots = module.calculate_capacity_slots(
            schedule, {"app": rows}, date(2026, 10, 1), date(2026, 10, 8),
            now=datetime(2026, 9, 27, tzinfo=timezone.utc))
        self.assertEqual(slots, [
            {"id": 101, "start": "2026-10-01 19:00", "end": "2026-10-01 20:00",
             "title": "CardioSculpt", "location": "Black Notley Village Hall",
             "capacity": 40, "booked": 32, "waiting": 0, "available": 8},
            {"id": 103, "start": "2026-10-02 09:00", "end": "2026-10-02 10:00",
             "title": "Jazzercise", "location": "Village Hall",
             "capacity": None, "booked": 8, "waiting": 0, "available": None},
            {"id": 107, "start": "2026-10-02 12:00", "end": "2026-10-02 13:00",
             "title": "Jazzercise", "location": "Village Hall",
             "capacity": 40, "booked": 40, "waiting": 2, "available": 2},
        ])

    def test_capacity_response_is_counted_and_truncated(self):
        rows = [self.row(1, 19, 101, 40, 32), self.row(2, 9, 103, 40, 17)]
        ajax = json.dumps({"app": rows}).encode()
        with mock.patch.object(module, "_get", side_effect=[
            (self.PAGE, self.URL), (ajax, "https://www.supersaas.nl/ajax/capacity/508523")
        ]):
            result = module.find_available_slots(self.URL, "2026-10-01", "2026-10-07",
                                                 max_results=1)
        self.assertEqual(result["unit"], "class")
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["slots"][0]["available"], 8)
        self.assertNotIn("duration_minutes", result)

    def test_capacity_window_and_malformed_rows(self):
        schedule = dataclasses.replace(module.parse_schedule(self.PAGE.decode(), self.URL),
                                       add_limit=3600, early_limit=86400)
        now = datetime(2026, 10, 1, 17, tzinfo=timezone.utc)
        rows = [self.row(1, 18, 101, 40, 1), self.row(1, 19, 102, 40, 1),
                self.row(2, 19, 103, 40, 1)]
        slots = module.calculate_capacity_slots(schedule, {"app": rows},
                                                date(2026, 10, 1), date(2026, 10, 3), now=now)
        self.assertEqual([slot["id"] for slot in slots], [102])
        with self.assertRaisesRegex(module.ScheduleError, "Malformed capacity slot"):
            module.calculate_capacity_slots(schedule, {"app": [[1, 2]]},
                                            date(2026, 10, 1), date(2026, 10, 3), now=now)


class HostCanonicalisationTests(unittest.TestCase):
    SLEEP = "https://www.supersaas.nl/schedule/downthehatch/SLEEP"

    def test_mobile_and_short_hosts_are_recognised_as_schedules(self):
        for host in ("www", "m", "d"):
            with self.subTest(host=host):
                self.assertIn(f"{host}.supersaas.nl", module.SCHEDULE_HOSTS)
                self.assertIn(f"{host}.supersaas.com", module.SCHEDULE_HOSTS)

    def test_any_host_reduces_to_the_www_form(self):
        cases = {
            "https://m.supersaas.com/schedule/demo/Shop": "https://www.supersaas.com/schedule/demo/Shop",
            "https://d.supersaas.com/schedule/demo/Shop": "https://www.supersaas.com/schedule/demo/Shop",
            "https://supersaas.nl/schedule/downthehatch/SLEEP": self.SLEEP,
            "https://www.supersaas.nl/schedule/downthehatch/SLEEP?view=week": self.SLEEP,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                resolved, linked_from = module.resolve_schedule_url(url)
                self.assertEqual(resolved, expected)
                self.assertIsNone(linked_from)

    def test_links_from_every_host_are_found_and_deduplicated(self):
        html = (
            '<a href="https://m.supersaas.nl/schedule/downthehatch/SLEEP">m</a>'
            '<a href="//www.supersaas.nl/schedule/downthehatch/SLEEP?view=week">www</a>'
            '<a href="https://d.supersaas.nl/schedule/downthehatch/SLEEP/">d</a>'
        )
        self.assertEqual(module.find_schedule_links(html), (self.SLEEP,))

    def test_trailing_slash_does_not_make_one_schedule_look_like_two(self):
        html = f'<a href="{self.SLEEP}">a</a><a href="{self.SLEEP}/">b</a>'
        self.assertEqual(module.find_schedule_links(html), (self.SLEEP,))

    def test_two_distinct_schedules_still_report_ambiguity(self):
        html = ('<a href="https://m.supersaas.nl/schedule/downthehatch/SLEEP">a</a>'
                '<a href="https://www.supersaas.com/schedule/other/SURF">b</a>')
        with mock.patch.object(module, "_get",
                               return_value=(html.encode(), "https://shop.example/")):
            with self.assertRaises(module.ScheduleError) as caught:
                module.resolve_schedule_url("https://shop.example/")
        message = str(caught.exception)
        self.assertIn(self.SLEEP, message)
        self.assertIn("https://www.supersaas.com/schedule/other/SURF", message)

    def test_sentence_punctuation_after_a_link_is_trimmed(self):
        self.assertEqual(module.find_schedule_links(
            '<p>Book at https://m.supersaas.nl/schedule/downthehatch/SLEEP.</p>'), (self.SLEEP,))
        self.assertEqual(module.find_schedule_links(
            f'<a href="{self.SLEEP.replace("www", "m")}">here</a>.'), (self.SLEEP,))


class ScheduleTypeTests(unittest.TestCase):
    URL = "https://www.supersaas.com/schedule/demo/Classes"
    CAPACITY = ('<script src="//assets.supersaas.net/assets/capacity-'
                '82a8c6eb1aa8b01938164e40c3a9dcb1684782bf5ea6.js"></script>'
                "<script>var open_times=[540,540,540,540,540,540,540,1080,1080,1080,1080,1080,1080,1080],"
                "overbooking=0,rp_name='Yoga'</script>")
    SERVICE = ('<script src="/assets/service-0123456789abcdef0123456789abcdef.js"></script>'
               "<script>var open_times=[540,540,540,540,540,540,540,1080,1080,1080,1080,1080,1080,1080]</script>")

    def test_capacity_schedule_without_public_slots_is_rejected(self):
        with self.assertRaises(module.ScheduleError) as caught:
            module.parse_schedule(self.CAPACITY, self.URL)
        message = str(caught.exception)
        self.assertIn("publishes no public slots", message)

    def test_service_schedule_is_named_separately(self):
        with self.assertRaises(module.ScheduleError) as caught:
            module.parse_schedule(self.SERVICE, self.URL)
        self.assertIn("service schedule", str(caught.exception))

    def test_resource_asset_does_not_trigger_the_type_error(self):
        html = ('<script src="/assets/resource-0123456789abcdef0123456789abcdef.js"></script>'
                "<html><head><title>User Login</title></head></html>")
        with self.assertRaises(module.ScheduleError) as caught:
            module.parse_schedule(html, self.URL)
        self.assertIn("behind a login", str(caught.exception))

    def test_empty_start_time_grid_reports_its_own_cause(self):
        html = ('<script src="/assets/resource-0123456789abcdef0123456789abcdef.js"></script>'
                "<script>var open_times=[540,540,540,540,540,540,540,1080,1080,1080,1080,1080,1080,1080],"
                "filter=9,resource[9]={data:[0,0,\"Studio\",1]},"
                "start=precalc_constraints(''),rounding=900,default_length=3600</script>")
        with self.assertRaises(module.ScheduleError) as caught:
            module.parse_schedule(html, self.URL)
        self.assertIn("no start-time grid", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
