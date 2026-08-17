"""Focused contracts for NovusAgenda meeting-list discovery."""

import asyncio
from datetime import datetime

from vendors.adapters.novusagenda_adapter_async import AsyncNovusAgendaAdapter


DAVIE_DUAL_GRID_HTML = """
<html><body>
  <table id="ctl00_SearchAgendasMeetings_radGridMeetings_ctl00">
    <thead><tr>
      <th></th><th>Meeting Date</th><th>Meeting Type</th>
      <th>Meeting Location</th><th>Online Agenda</th>
    </tr></thead>
    <tbody>
      <tr class="rgRow" id="radGridMeetings_ctl00__0">
        <td></td><td>08/19/26</td><td>Regular Council Meeting</td>
        <td>Town Hall</td>
        <td><a href="DisplayAgendaPDF.ashx?MeetingID=653">Agenda PDF</a></td>
      </tr>
      <tr class="rgAltRow" id="radGridMeetings_ctl00__1">
        <td></td><td>07/22/26</td><td>Workshop Meeting</td>
        <td>Town Hall</td><td></td>
      </tr>
    </tbody>
  </table>
  <table id="ctl00_SearchAgendasMeetings_radGridItems_ctl00">
    <thead><tr>
      <th>Date</th><th>Title</th><th>Category</th><th>Meeting Type</th>
    </tr></thead>
    <tbody>
      <tr class="rgRow" id="radGridItems_ctl00__0">
        <td>08/19/26</td><td>FY 2026 Budget Amendment 3</td>
        <td>ORDINANCES SECOND READING</td><td>Regular Council Meeting</td>
        <td><a href="CoverSheet.aspx?ItemID=9426&amp;MeetingID=653">Item</a></td>
      </tr>
    </tbody>
  </table>
</body></html>
"""


LEGACY_GENERIC_GRID_HTML = """
<html><body><table><tbody>
  <tr class="rgRow">
    <td>08/19/2026</td><td>Planning Commission</td><td>6:30 PM</td>
    <td><a href="DisplayAgendaPDF.ashx?MeetingID=900">Agenda PDF</a></td>
  </tr>
</tbody></table></body></html>
"""


MINUTES_GRID_HTML = """
<html><body>
  <table id="ctl00_SearchAgendasMeetings_radGridMeetings_ctl00">
    <thead><tr><th>Meeting Date</th><th>Meeting Type</th><th>Minutes</th></tr></thead>
    <tbody><tr class="rgRow">
      <td>08/05/26</td><td>Regular Council Meeting</td>
      <td><a onclick="window.open('MeetingView.aspx?MeetingID=652&amp;MinutesMeetingID=700&amp;doctype=Minutes')">View Minutes HTML</a></td>
    </tr></tbody>
  </table>
</body></html>
"""


class _Response:
    def __init__(self, html: str):
        self.html = html

    async def text(self) -> str:
        return self.html


class _FixtureNovusAdapter(AsyncNovusAgendaAdapter):
    def __init__(self, html: str):
        super().__init__("davie")
        self.html = html

    async def _get(self, url: str, **kwargs):
        del url, kwargs
        return _Response(self.html)

    def _date_range(self, days_back: int, days_forward: int):
        del days_back, days_forward
        return datetime(2026, 1, 1), datetime(2026, 12, 31)


def test_davie_dual_grid_uses_only_meeting_rows_and_emits_iso_dates():
    result = asyncio.run(_FixtureNovusAdapter(DAVIE_DUAL_GRID_HTML).fetch_meetings())

    assert result.success is True
    assert [meeting["title"] for meeting in result.meetings] == [
        "Regular Council Meeting",
        "Workshop Meeting",
    ]
    assert result.meetings[0]["vendor_id"] == "653"
    assert result.meetings[0]["start"] == "2026-08-19"
    assert result.meetings[0]["packet_url"] == (
        "https://davie.novusagenda.com/agendapublic/"
        "DisplayAgendaPDF.ashx?MeetingID=653"
    )
    assert result.meetings[1]["start"] == "2026-07-22"
    assert all("Budget Amendment" not in meeting["title"] for meeting in result.meetings)


def test_legacy_generic_rows_remain_supported_and_preserve_available_time():
    result = asyncio.run(
        _FixtureNovusAdapter(LEGACY_GENERIC_GRID_HTML).fetch_meetings()
    )

    assert result.success is True
    assert len(result.meetings) == 1
    assert result.meetings[0]["vendor_id"] == "900"
    assert result.meetings[0]["start"] == "2026-08-19T18:30"


def test_minutes_discovery_uses_iso_date_and_native_meeting_id():
    result = asyncio.run(_FixtureNovusAdapter(MINUTES_GRID_HTML).fetch_minutes())

    assert result.success is True
    assert result.meetings == [
        {
            "vendor_id": "652",
            "title": "Regular Council Meeting",
            "start": "2026-08-05",
            "minutes_url": (
                "https://davie.novusagenda.com/agendapublic/"
                "MeetingView.aspx?MeetingID=652&MinutesMeetingID=700&doctype=Minutes"
            ),
        }
    ]
