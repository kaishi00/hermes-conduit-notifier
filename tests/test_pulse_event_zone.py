"""A calendar event made in another time zone is told in the zone the
report reads in, everywhere Pulse tells or matches it
(hermes-conduit-notifier#1138, #1140)."""

from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from test_pulse import pulse
from test_pulse_brief_open import brief
from test_pulse_dates import at
from test_pulse_followup import live  # noqa: F401
from test_pulse_hours import phone_home

dates = pulse.report.pulse_dates
PARIS = ZoneInfo("Europe/Paris")
TOKYO_FIVE = "2026-11-27T17:00:00+09:00"


def test_a_moment_with_an_offset_moves_and_a_date_or_floating_time_stays():
    assert dates.in_zone(TOKYO_FIVE, PARIS).isoformat() == "2026-11-27T09:00:00+01:00"
    assert dates.in_zone("2026-11-27T08:00:00Z", PARIS).isoformat() == "2026-11-27T09:00:00+01:00"
    for same in ("2026-11-27", "2026-11-27T17:00:00", "", None, "not a time"):
        assert dates.in_zone(same, PARIS) is None
    assert dates.in_zone(TOKYO_FIVE, None) is None


def test_an_events_day_is_the_one_it_falls_on_in_the_readers_zone():
    def day(start, **more):
        return dates.event_day({"start": start, **more}, PARIS)
    assert day("2026-11-27T01:30:00+09:00") == "2026-11-26"  # the evening before
    assert day(TOKYO_FIVE) == "2026-11-27"
    assert day("2026-11-27") == "2026-11-27"
    assert day("2026-11-27T01:30:00") == "2026-11-27"  # floating: as written
    assert day("2026-11-27T00:00:00+09:00", all_day=True) == "2026-11-27"  # all day: a date


def test_where_an_event_was_made_is_said_only_when_its_clock_differs():
    moved = dates.in_zone(TOKYO_FIVE, PARIS)
    assert dates.elsewhere(TOKYO_FIVE, moved, "Asia/Tokyo") == "Asia/Tokyo"
    assert dates.elsewhere(TOKYO_FIVE, moved, None) == "UTC+09:00"  # no zone name synced: its offset
    # A zone with no name (the host's own, when the phone gives none) still gets the note.
    host = dates.in_zone(TOKYO_FIVE, timezone(timedelta(hours=1)))
    assert dates.elsewhere(TOKYO_FIVE, host, "Asia/Tokyo") == "Asia/Tokyo"
    # The same clock (the phone's own zone, or another with its offset): nothing to say.
    local = "2026-11-27T09:00:00+01:00"
    assert dates.elsewhere(local, dates.in_zone(local, PARIS), "Europe/Paris") == ""
    assert dates.elsewhere(local, dates.in_zone(local, PARIS), "Africa/Lagos") == ""
    assert dates.elsewhere("2026-11-27", None, "Asia/Tokyo") == ""


def test_todays_events_are_told_at_their_time_in_the_briefs_zone():
    event = {"title": "Tour", "start": TOKYO_FIVE}
    assert pulse.places._event_when(event, PARIS) == "09:00"
    assert pulse.places._event_when({"start": "2026-11-27T17:00:00"}, PARIS) == "17:00"  # floating
    assert pulse.places._event_when({"start": "soon"}, PARIS) == ""


def test_the_morning_brief_tells_the_calendar_in_its_own_zone(live):
    phone_home(live.home)  # a London phone: Standup at 09:30+01:00 on the 14th
    text = brief(live, at(14, 7))  # read in UTC
    assert "Standup (2026-10-14T08:30:00+00:00; event set in Europe/London;" in text


def test_a_card_matched_to_an_event_takes_the_day_it_falls_on_for_the_reader():
    late = {"id": "e1", "title": "Flight to Osaka", "start": "2026-11-27T01:30:00+09:00", "time_zone": "Asia/Tokyo"}
    eve = pulse.candidates._due("2026-11-26", PARIS)
    assert pulse.candidates._event_named([late], eve, PARIS, "Flight to Osaka tonight") is late
    assert pulse.candidates._event_named([late], pulse.candidates._due("2026-11-27", PARIS), PARIS,
                                         "Flight to Osaka tonight") is None
