from datetime import date, datetime, timedelta

import pytest

from pipeline.pulse import (
    MISSING_RECHECK_SECONDS,
    PROBE_CEILING_SECONDS,
    PROBE_FLOOR_SECONDS,
    SignalMissing,
    boardbook_rows,
    civicclerk_activity,
    civicplus_search_rows,
    civicweb_rows,
    destiny_rows,
    diff_rows,
    escribe_rows,
    extract_feed_keys,
    failure_interval,
    is_due,
    parse_json_body,
    parse_legistar_time,
    parse_title_date,
    targeted_range,
    quiet_interval,
)

GRANICUS_FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><guid isPermaLink="false">a</guid><title>Council - Sep 30</title>
<pubDate>Fri, 25 Sep 2026 11:01:46 -0700</pubDate></item>
<item><guid isPermaLink="false">b</guid><title>Parks - Oct 1</title>
<pubDate>Thu, 24 Sep 2026 10:09:36 -0700</pubDate></item>
</channel></rss>"""


def test_feed_keys_one_per_item():
    keys = extract_feed_keys(GRANICUS_FEED)
    assert keys is not None and len(keys) == 2


def test_republished_item_changes_its_key():
    republished = GRANICUS_FEED.replace("Fri, 25 Sep 2026 11:01:46", "Sat, 26 Sep 2026 09:00:00")
    assert sorted(extract_feed_keys(GRANICUS_FEED) or []) != sorted(extract_feed_keys(republished) or [])


def test_html_is_not_a_feed():
    assert extract_feed_keys("<!doctype html><html><body>RSS</body></html>") is None


def test_empty_feed_has_no_keys():
    assert extract_feed_keys("<rss><channel></channel></rss>") == []


def test_non_json_endpoint_is_missing_signal():
    with pytest.raises(SignalMissing):
        parse_json_body("<html>maintenance</html>")


def test_iqm2_rendered_feed_entries():
    body = (
        '<?xml version="1.0" encoding="utf-16"?><html><head><title>RSS Feed</title></head><body>'
        "<h2>City Council - Agenda - Sep 29, 2026 6:00 PM</h2><p><a href='Detail_Meeting.aspx?ID=5464'>x</a></p>"
        "<h2>City Council - Minutes - Sep 22, 2026 12:00 PM</h2><p><a href='FileOpen.aspx?ID=9'>y</a></p>"
        "</body></html>"
    )
    keys = extract_feed_keys(body)
    assert keys is not None and len(keys) == 2
    assert parse_title_date(keys[0]) == date(2026, 9, 29)


def test_rows_new_or_moved_row_is_change_with_date():
    base = escribe_rows({"d": [{"ID": "a", "StartDate": "2026/10/01 09:30:00", "HasAgenda": False}]})
    first = diff_rows(None, base)
    assert not first.changed
    moved = escribe_rows({"d": [{"ID": "a", "StartDate": "2026/10/01 09:30:00", "HasAgenda": True}]})
    reading = diff_rows(first.state["rows"], moved)
    assert reading.changed and reading.hint_dates == (date(2026, 10, 1),)


def test_rows_leaving_window_is_not_a_change():
    rows = escribe_rows({"d": [{"ID": "a", "StartDate": "2026/10/01 09:30:00"},
                               {"ID": "b", "StartDate": "2026/10/02 09:30:00"}]})
    state = diff_rows(None, rows).state["rows"]
    assert not diff_rows(state, rows[1:]).changed


def test_legistar_time_variable_precision():
    assert parse_legistar_time("2026-09-25T21:23:11.46") > parse_legistar_time("2026-09-25T21:23:11.457")


def test_quiet_interval_stretches_to_ceiling():
    assert quiet_interval(0) == PROBE_FLOOR_SECONDS
    assert PROBE_FLOOR_SECONDS < quiet_interval(3) < PROBE_CEILING_SECONDS
    assert quiet_interval(10_000) == PROBE_CEILING_SECONDS


def test_failure_interval_backs_off_and_caps():
    assert failure_interval(1, unusable=False) == PROBE_FLOOR_SECONDS
    assert failure_interval(2, unusable=False) == 2 * PROBE_FLOOR_SECONDS
    assert failure_interval(50, unusable=False) == MISSING_RECHECK_SECONDS
    assert failure_interval(0, unusable=True) == MISSING_RECHECK_SECONDS


def test_is_due():
    now = datetime(2026, 9, 27, 12, 0)
    assert is_due(None, now)
    assert is_due({"next_probe_at": None}, now)
    assert is_due({"next_probe_at": now - timedelta(seconds=1)}, now)
    assert not is_due({"next_probe_at": now + timedelta(minutes=5)}, now)


def test_civicclerk_activity_takes_latest_publication():
    event = {
        "createdOn": "2026-09-10T00:00:00Z",
        "publishStart": "2026-09-11T00:00:00Z",
        "publishedFiles": [{"publishOn": "2026-09-18T09:03:52.76Z"}, {"publishOn": None}],
    }
    assert civicclerk_activity(event) == "2026-09-18T09:03:52.76Z"


def test_civicclerk_activity_ignores_sentinel_dates():
    assert civicclerk_activity({"createdOn": "0001-01-01T00:00:00Z", "publishedFiles": []}) is None


def test_parse_title_date_granicus_style():
    assert parse_title_date("Historic Preservation - Sep 30, 2026|guid=a") == date(2026, 9, 30)
    assert parse_title_date("Council - September 3, 2026") == date(2026, 9, 3)
    assert parse_title_date("T.R.C. Meeting Agenda") is None


def test_targeted_range_pads_and_caps():
    start, end = targeted_range([date(2026, 10, 1), date(2026, 10, 5)])
    assert (start.date(), end.date()) == (date(2026, 9, 30), date(2026, 10, 7))
    assert targeted_range([]) is None
    assert targeted_range([date(2026, 1, 1), date(2026, 6, 1)]) is None


def test_civicplus_search_rows_date_and_id():
    html = (
        '<div class="AgendaCenter"><table><tr id="row1" class="catAgendaRow"><td><h3>'
        '<a id="_09282026-380" name="_09282026-380"></a> Sep 28, 2026 Posted Sep 23, 2026 2:32 PM</h3>'
        '<a href="/AgendaCenter/ViewFile/Agenda/_09282026-380">Agenda</a></td></tr></table></div>'
    )
    rows = civicplus_search_rows(html)
    assert [(row_id, day) for row_id, day, _ in rows] == [("380", date(2026, 9, 28))]


def test_destiny_rows_seq_and_date():
    html = (
        '<table id="meeting-table"><tr><td><a href="/agenda_publish.cfm&#x3f;id&#x3d;1&amp;dsp&#x3d;ag&amp;seq&#x3d;6269" '
        'title="View Agenda for Arts Commission (09/29/2026)">September 29, 2026</a></td><td>Arts</td></tr></table>'
    )
    assert [(row_id, day) for row_id, day, _ in destiny_rows(html)] == [("6269", date(2026, 9, 29))]


def test_civicweb_publish_flip_is_a_change():
    row = {"Id": 1, "MeetingDate": "2026-10-02", "Published": False, "Name": "Board", "VideoIcon": ""}
    state = diff_rows(None, civicweb_rows([row])).state["rows"]
    assert not diff_rows(state, civicweb_rows([{**row, "VideoIcon": "x"}])).changed
    assert diff_rows(state, civicweb_rows([{**row, "Published": True}])).changed


def test_boardbook_rows_dates_and_ids():
    html = (
        "<h2>Search Results</h2><ul>"
        '<li><div><a href="/Search/GoToResult/769369?Index=sparqmeetings&amp;PublicOrgID=963">'
        "Meeting: September 25, 2026 at 7:00 PM - Schedule of Meetings</a></div></li>"
        '<li><div><a href="/Search/GoToResult/18874858?Index=sparqmeetingsagendaitems&amp;PublicOrgID=963">'
        "Agenda Item: Oct 1 2026 4:30PM -- Policy Committee <br /> 709 Policy</a></div></li>"
        '<li><div><a href="/Search/GoToResult/69a1c561-1e0f?Index=sparqmeetingsdocuments&amp;PublicOrgID=963">'
        "File / Attachment / Document: Resolution</a></div></li></ul>"
    )
    rows = {row_id: day for row_id, day, _ in boardbook_rows(html)}
    assert rows == {
        "sparqmeetings:769369": date(2026, 9, 25),
        "sparqmeetingsagendaitems:18874858": date(2026, 10, 1),
        "sparqmeetingsdocuments:69a1c561-1e0f": None,
    }
