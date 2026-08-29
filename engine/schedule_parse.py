
"""Schedule expression parsing for the spec-sheet scheduler.

Supported schedule strings (all case-insensitive):

    "*-style:  manual, now
    "interval:<seconds>"       e.g. interval:3600
    "at:<ISO-8601>"            e.g. at:2026-08-29T03:00:00Z
    "cron:<5-field expr>"      e.g. cron:0 9 * * 1-5   (M H DoM Mon DoW)

This module deliberately avoids a third-party cron dependency. We implement a
minimal 5-field cron parser covering the field forms that appear in real
spec sheets (lists, ranges, steps, "*", "?", and month/day-of-week names)
and a next-due computation that is correct to the minute.

The module is pure: no I/O, no state, no logging — so it is trivially
unit-testable and can be imported from the webui or any adapter layer.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple


class ScheduleError(ValueError):
    """Raised when a schedule string cannot be parsed."""


# ---------------------------------------------------------------------------
# Field tables for cron
# ---------------------------------------------------------------------------

_MONTH_NAMES = {name: i + 1 for i, name in enumerate([
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
])}
_DOW_NAMES = {name: i for i, name in enumerate([
    "sun", "mon", "tue", "wed", "thu", "fri", "sat",
])}


def _cron_parse_field(raw: str, low: int, high: int, *, name_map: Optional[Dict[str, int]] = None) -> List[int]:
    """Parse one cron field (minute/hour/dom/mon/dow) into a sorted list of ints.

    Supports:  *   N  N-M   N-M/S  A,*  A,B,C  A-M/S  (with optional names)
    Raises ScheduleError on anything out of range or malformed.
    """
    allowed: set[int] = set()
    name_map = name_map or {}

    def _resolve(token: str) -> int:
        token = token.strip().lower()
        if token in name_map:
            return name_map[token]
        try:
            return int(token)
        except ValueError as exc:
            raise ScheduleError(f"Unknown symbol in cron field: {token!r}") from exc

    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ScheduleError("Empty cron field component")
        step = 1
        body = part
        if "/" in part:
            body, step_raw = part.split("/", 1)
            try:
                step = int(step_raw)
            except ValueError:
                raise ScheduleError(f"Invalid cron step: {step_raw!r}")
            if step <= 0:
                raise ScheduleError(f"Cron step must be > 0: {step}")

        if body == "*" or body == "?":
            base = list(range(low, high + 1))
        elif "-" in body:
            a, b = body.split("-", 1)
            a_val, b_val = _resolve(a), _resolve(b)
            if a_val < low or a_val > high or b_val < low or b_val > high:
                raise ScheduleError(f"Cron range {body!r} out of bounds [{low},{high}]")
            if a_val > b_val:
                raise ScheduleError(f"Cron range {body!r} has inverted endpoints")
            base = list(range(a_val, b_val + 1))
        else:
            v = _resolve(body)
            if v < low or v > high:
                raise ScheduleError(f"Cron value {v} out of bounds [{low},{high}]")
            base = [v]
        if step > 1:
            base = base[::step]
        allowed.update(base)
    if not allowed:
        raise ScheduleError(f"Empty cron field: {raw!r}")
    return sorted(allowed)


class CronSchedule:
    """Five-field cron schedule with a next-due computation.

    Day-of-week: 0 or 7 both mean Sunday (standard cron + Vixie extension).
    Day-of-month and day-of-week both being restricted => OR (cron quirk);
    if only one is restricted, AND.
    """

    def __init__(self, expression: str) -> None:
        fields = expression.strip().split()
        if len(fields) != 5:
            raise ScheduleError(f"Cron expression must have exactly 5 fields, got {len(fields)}: {expression!r}")
        self.raw = expression
        self.minutes = _cron_parse_field(fields[0], 0, 59)
        self.hours = _cron_parse_field(fields[1], 0, 23)
        self.days_of_month = _cron_parse_field(fields[2], 1, 31)
        self.months = _cron_parse_field(fields[3], 1, 12, name_map=_MONTH_NAMES)
        self.days_of_week = _cron_parse_field(fields[4], 0, 7, name_map=_DOW_NAMES)
        if 7 in self.days_of_week:
            self.days_of_week = sorted({d % 7 for d in self.days_of_week})
        # Track whether the user explicitly wrote non-star for dom/dow so we
        # can apply the cron OR quirk.
        self._dom_is_star = fields[2] in ("*", "?")
        self._dow_is_star = fields[4] in ("*", "?")

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minutes:
            return False
        if dt.hour not in self.hours:
            return False
        if dt.month not in self.months:
            return False
        dom_ok = dt.day in self.days_of_month
        # Python: Monday=0..Sunday=6. Cron: Sunday=0..Saturday=6.
        dow = (dt.weekday() + 1) % 7
        dow_ok = dow in self.days_of_week
        if not self._dom_is_star and not self._dow_is_star:
            return dom_ok or dow_ok   # cron OR quirk
        if not self._dom_is_star:
            return dom_ok and dow_ok if self._dow_is_star else dom_ok
        if not self._dow_is_star:
            return dom_ok and dow_ok
        return True

    def previous_due(self, before: datetime) -> Optional[datetime]:
        """Return the most recent due time at-or-before ``before``, rounded to
        the minute. Returns None if no due time exists in the one-year window
        ending at ``before`` (very rarely happens for a valid cron)."""
        if before.tzinfo is None:
            before = before.replace(tzinfo=timezone.utc)
        candidate = before.astimezone(timezone.utc).replace(second=0, microsecond=0)
        floor = candidate - timedelta(days=366)
        # Walk backwards minute-by-minute. At the worst case (a sparse cron
        # like ``0 0 1 1 *``) this can be ~527k iterations, so we use a
        # month/day jump strategy to keep it fast.
        while candidate >= floor:
            if candidate.month not in self.months:
                # jump to first of previous month (UTC-safe)
                first_this_month = candidate.replace(day=1, hour=0, minute=0)
                candidate = first_this_month - timedelta(days=1)
                candidate = candidate.replace(second=0, microsecond=0)
                continue
            # jump to last day-of-hour if hour not matched
            if candidate.hour not in self.hours:
                candidate = candidate.replace(minute=0) + timedelta(hours=-1)
                continue
            if candidate.minute not in self.minutes:
                candidate += timedelta(minutes=-1)
                continue
            dow = (candidate.weekday() + 1) % 7
            dom_ok = candidate.day in self.days_of_month
            dow_ok = dow in self.days_of_week
            if not self._dom_is_star and not self._dow_is_star:
                day_ok = dom_ok or dow_ok
            elif not self._dom_is_star:
                day_ok = dom_ok
            elif not self._dow_is_star:
                day_ok = dow_ok
            else:
                day_ok = True
            if not day_ok:
                candidate += timedelta(days=-1)
                candidate = candidate.replace(hour=0, minute=0)
                continue
            return candidate
        return None

    def next_due(self, after: datetime) -> datetime:
        """Return the next due time strictly after `after`, rounded to the
        minute. Raises ScheduleError after one year of search with no match.
        """
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        candidate = after.astimezone(timezone.utc)
        candidate = candidate.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = candidate + timedelta(days=366)
        dom_restricted = not self._dom_is_star
        dow_restricted = not self._dow_is_star

        while candidate < limit:
            if candidate.month not in self.months:
                if candidate.month == 12:
                    candidate = candidate.replace(day=1, hour=0, minute=0, year=candidate.year + 1)
                else:
                    candidate = candidate.replace(day=1, hour=0, minute=0, month=candidate.month + 1)
                continue
            dow = (candidate.weekday() + 1) % 7
            if dom_restricted and dow_restricted:
                day_ok = (candidate.day in self.days_of_month) or (dow in self.days_of_week)
            elif dom_restricted:
                day_ok = candidate.day in self.days_of_month
            elif dow_restricted:
                day_ok = dow in self.days_of_week
            else:
                day_ok = True
            if not day_ok:
                candidate = candidate.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if candidate.hour not in self.hours:
                candidate = candidate.replace(minute=0) + timedelta(hours=1)
                continue
            if candidate.minute not in self.minutes:
                candidate += timedelta(minutes=1)
                continue
            return candidate
        raise ScheduleError(f"No next due time found for cron expression within a year: {self.raw!r}")


# ---------------------------------------------------------------------------
# Unified schedule
# ---------------------------------------------------------------------------


class ParsedSchedule:
    """A parsed schedule string; exposes `kind` + `next_due(after)`."""

    KIND_MANUAL = "manual"
    KIND_NOW = "now"
    KIND_AT = "at"
    KIND_INTERVAL = "interval"
    KIND_CRON = "cron"

    def __init__(self, expression: str) -> None:
        self.expression = expression
        self.kind: str = self.KIND_MANUAL
        self.interval_seconds: Optional[int] = None
        self.at: Optional[datetime] = None
        self.cron: Optional[CronSchedule] = None
        self._parse()

    def _parse(self) -> None:
        raw = self.expression.strip()
        if raw.lower() in ("manual", ""):
            self.kind = self.KIND_MANUAL
            return
        if raw.lower() in ("now", "immediate"):
            self.kind = self.KIND_NOW
            return
        if raw.startswith("interval:"):
            try:
                self.interval_seconds = int(raw.split(":", 1)[1])
            except ValueError as exc:
                raise ScheduleError(f"Invalid interval seconds: {raw!r}") from exc
            if self.interval_seconds <= 0:
                raise ScheduleError("Interval must be > 0 seconds")
            self.kind = self.KIND_INTERVAL
            return
        if raw.startswith("at:"):
            iso = raw.split(":", 1)[1]
            try:
                self.at = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ScheduleError(f"Invalid ISO-8601 at-time: {iso!r}") from exc
            if self.at.tzinfo is None:
                self.at = self.at.replace(tzinfo=timezone.utc)
            self.kind = self.KIND_AT
            return
        if raw.startswith("cron:"):
            expr = raw.split(":", 1)[1]
            self.cron = CronSchedule(expr)
            self.kind = self.KIND_CRON
            return
        raise ScheduleError(f"Unrecognized schedule expression: {self.expression!r}")

    def next_due(self, after: datetime) -> Optional[datetime]:
        """Return the next due time strictly after `after`, or None if the
        schedule is manual (no automatic due). For `at` schedules that are
        already in the past, return None.
        """
        after = after.astimezone(timezone.utc) if after.tzinfo else after.replace(tzinfo=timezone.utc)
        if self.kind == self.KIND_MANUAL:
            return None
        if self.kind == self.KIND_NOW:
            return after
        if self.kind == self.KIND_AT:
            return self.at if self.at is not None and self.at > after else None
        if self.kind == self.KIND_INTERVAL:
            return after + timedelta(seconds=self.interval_seconds)  # type: ignore[operator]
        if self.kind == self.KIND_CRON and self.cron is not None:
            return self.cron.next_due(after)
        return None
