"""Tests for engine/schedule_parse.py — schedule expression parser.

Covers the five supported kinds (manual/now/at/interval/cron) plus
edge cases (invalid inputs, cron field syntax, DOM/DOW OR quirk,
UTC normalization).
"""
import unittest
from datetime import datetime, timedelta, timezone

from engine.schedule_parse import (
    CronSchedule,
    ParsedSchedule,
    ScheduleError,
)


class TestParsedScheduleBasics(unittest.TestCase):
    def test_manual_schedule(self):
        p = ParsedSchedule("manual")
        self.assertEqual(p.kind, ParsedSchedule.KIND_MANUAL)
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(p.next_due(now))

    def test_now_schedule_returns_after(self):
        p = ParsedSchedule("now")
        self.assertEqual(p.kind, ParsedSchedule.KIND_NOW)
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(p.next_due(now), now)

    def test_interval_schedule(self):
        p = ParsedSchedule("interval:3600")
        self.assertEqual(p.kind, ParsedSchedule.KIND_INTERVAL)
        self.assertEqual(p.interval_seconds, 3600)
        now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            p.next_due(now),
            now + timedelta(seconds=3600),
        )

    def test_invalid_interval_string(self):
        with self.assertRaises(ScheduleError):
            ParsedSchedule("interval:abc")

    def test_interval_must_be_positive(self):
        with self.assertRaises(ScheduleError):
            ParsedSchedule("interval:0")
        with self.assertRaises(ScheduleError):
            ParsedSchedule("interval:-1")

    def test_at_schedule_iso8601(self):
        p = ParsedSchedule("at:2026-08-29T03:00:00Z")
        self.assertEqual(p.kind, ParsedSchedule.KIND_AT)
        self.assertEqual(p.at, datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc))

    def test_at_past_returns_none(self):
        p = ParsedSchedule("at:2026-01-01T00:00:00Z")
        now = datetime(2026, 8, 28, tzinfo=timezone.utc)
        self.assertIsNone(p.next_due(now))

    def test_unrecognized_kind_raises(self):
        with self.assertRaises(ScheduleError):
            ParsedSchedule("banana:12")

    def test_empty_expression_is_manual(self):
        p = ParsedSchedule("")
        self.assertEqual(p.kind, ParsedSchedule.KIND_MANUAL)


class TestCronScheduleFields(unittest.TestCase):
    def test_star_field(self):
        c = CronSchedule("* * * * *")
        self.assertEqual(c.minutes, list(range(60)))
        self.assertEqual(c.hours, list(range(24)))

    def test_single_value(self):
        c = CronSchedule("15 9 * * *")
        self.assertEqual(c.minutes, [15])
        self.assertEqual(c.hours, [9])

    def test_range(self):
        c = CronSchedule("0 9-17 * * *")
        self.assertEqual(c.hours, list(range(9, 18)))

    def test_list(self):
        c = CronSchedule("0,30 * * * *")
        self.assertEqual(c.minutes, [0, 30])

    def test_step_in_range(self):
        c = CronSchedule("0-59/15 * * * *")
        self.assertEqual(c.minutes, [0, 15, 30, 45])

    def test_month_names(self):
        c = CronSchedule("0 9 * jan,jul *")
        self.assertEqual(c.months, [1, 7])

    def test_star_step_minutes(self):
        c = CronSchedule("*/15 * * * *")
        self.assertEqual(c.minutes, [0, 15, 30, 45])

    def test_day_of_week_names(self):
        c = CronSchedule("0 9 * * mon,fri")
        self.assertEqual(c.days_of_week, [1, 5])

    def test_sunday_as_seven_wraps(self):
        c = CronSchedule("0 9 * * 7")
        self.assertEqual(c.days_of_week, [0])

    def test_out_of_range_raises(self):
        with self.assertRaises(ScheduleError):
            CronSchedule("60 * * * *")
        with self.assertRaises(ScheduleError):
            CronSchedule("* 24 * * *")
        with self.assertRaises(ScheduleError):
            CronSchedule("* * 0 * *")
        with self.assertRaises(ScheduleError):
            CronSchedule("* * * 13 *")
        with self.assertRaises(ScheduleError):
            CronSchedule("* * * * 8")
        with self.assertRaises(ScheduleError):
            CronSchedule("bad expr here at all please thank you")

    def test_inverted_range_raises(self):
        with self.assertRaises(ScheduleError):
            CronSchedule("9-5 * * * *")

    def test_empty_component_raises(self):
        with self.assertRaises(ScheduleError):
            CronSchedule("0,,30 * * * *")

    def test_not_five_fields(self):
        with self.assertRaises(ScheduleError):
            CronSchedule("0 9 * *")
        with self.assertRaises(ScheduleError):
            CronSchedule("0 9 * * * *")


class TestCronMatches(unittest.TestCase):
    def test_every_minute(self):
        c = CronSchedule("* * * * *")
        for h in range(0, 24, 4):
            for m in range(0, 60, 15):
                dt = datetime(2026, 8, 28, h, m, tzinfo=timezone.utc)
                self.assertTrue(c.matches(dt), f"failed at {dt}")

    def test_specific_minute(self):
        c = CronSchedule("30 * * * *")
        self.assertTrue(c.matches(datetime(2026, 8, 28, 10, 30, tzinfo=timezone.utc)))
        self.assertFalse(c.matches(datetime(2026, 8, 28, 10, 31, tzinfo=timezone.utc)))

    def test_specific_hour(self):
        c = CronSchedule("30 9 * * *")
        self.assertTrue(c.matches(datetime(2026, 8, 28, 9, 30, tzinfo=timezone.utc)))
        self.assertFalse(c.matches(datetime(2026, 8, 28, 10, 30, tzinfo=timezone.utc)))

    def test_specific_dow(self):
        # 2026-08-28 is a Friday (weekday()=4, cron dow=5)
        c = CronSchedule("0 9 * * fri")
        self.assertTrue(c.matches(datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)))
        self.assertFalse(c.matches(datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)))

    def test_dom_dow_or_quirk(self):
        # Both DOM and DOW restricted => OR (standard cron behavior)
        c = CronSchedule("0 9 1 * mon")
        # 2026-08-01 is a Saturday — should match (DOM=1)
        self.assertTrue(c.matches(datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)))
        # 2026-08-03 is a Monday — should match (DOW=mon)
        self.assertTrue(c.matches(datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)))
        # 2026-08-02 is a Sunday, day 2 — should NOT match
        self.assertFalse(c.matches(datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)))

    def test_dom_only(self):
        c = CronSchedule("0 9 15 * *")
        self.assertTrue(c.matches(datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc)))
        self.assertFalse(c.matches(datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)))


class TestCronNextDue(unittest.TestCase):
    def test_every_minute_advances_one(self):
        c = CronSchedule("* * * * *")
        after = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(c.next_due(after), datetime(2026, 8, 28, 12, 1, 0, tzinfo=timezone.utc))

    def test_specific_minute_next(self):
        c = CronSchedule("30 * * * *")
        after = datetime(2026, 8, 28, 12, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(c.next_due(after), datetime(2026, 8, 28, 12, 30, 0, tzinfo=timezone.utc))

    def test_specific_hour_next(self):
        c = CronSchedule("30 9 * * *")
        after = datetime(2026, 8, 28, 9, 31, 0, tzinfo=timezone.utc)
        self.assertEqual(c.next_due(after), datetime(2026, 8, 29, 9, 30, 0, tzinfo=timezone.utc))

    def test_specific_dow_next(self):
        c = CronSchedule("0 9 * * fri")
        # after Fri 2026-08-28 09:01 => next Fri 2026-09-04 09:00
        after = datetime(2026, 8, 28, 9, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(c.next_due(after), datetime(2026, 9, 4, 9, 0, 0, tzinfo=timezone.utc))

    def test_navigate_to_existing_31st(self):
        # Aug 31 exists, so the next 31st at 09:00 is 2026-08-31.
        c = CronSchedule("0 9 31 * *")
        after = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(c.next_due(after), datetime(2026, 8, 31, 9, 0, 0, tzinfo=timezone.utc))

    def test_navigate_to_october_31(self):
        # After 2026-10-31 09:00 the next 31st is 2026-12-31 (Nov has 30 days).
        c = CronSchedule("0 9 31 * *")
        after = datetime(2026, 10, 31, 9, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(c.next_due(after), datetime(2026, 12, 31, 9, 0, 0, tzinfo=timezone.utc))

    def test_year_rollover(self):
        c = CronSchedule("0 9 31 12 *")  # Dec 31 at 09:00
        after = datetime(2026, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(c.next_due(after), datetime(2027, 12, 31, 9, 0, 0, tzinfo=timezone.utc))

    def test_impossible_expression_raises(self):
        # "Feb 30" is impossible — should raise after a year
        c = CronSchedule("0 9 30 2 *")
        with self.assertRaises(ScheduleError):
            c.next_due(datetime(2026, 1, 1, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
