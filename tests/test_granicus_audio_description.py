from vendors.adapters.parsers.granicus_parser import parse_viewpublisher_listing


def row(clip, title, date, extra=''):
    return f'''<tr class="odd"><td class="listItem">{title}</td>
    <td class="listItem">{date}</td><td class="listItem">
    <a href="/AgendaViewer.php?view_id=25&amp;clip_id={clip}">Agenda</a>
    {extra}</td></tr>'''


def parse(*rows):
    return parse_viewpublisher_listing(
        '<table>' + ''.join(rows) + '</table>', 'https://pasadena.granicus.com',
    )


def test_accessible_reupload_uses_title_date_and_preserves_minutes():
    meetings = parse(
        row('2', 'City Council Meeting: August 24, 2026 (Audio Description)',
            'Aug 27, 2026 - 08:15 AM', '<a href="/minutes/2.pdf">Minutes</a>'),
        row('1', 'City Council Meeting: August 24, 2026',
            'Aug 24, 2026 - 06:03 PM'),
        row('3', 'City Council Meeting: August 31, 2026',
            'Aug 31, 2026 - 06:19 PM'),
    )
    assert [m['event_id'] for m in meetings] == ['1', '3']
    assert meetings[0]['start'] == '2026-08-24T18:03:00'
    assert meetings[0]['minutes_url'] == 'https://pasadena.granicus.com/minutes/2.pdf'


def test_lone_accessible_and_audio_only_sessions_are_retained():
    meetings = parse(
        row('1', 'City Council Meeting: September 15, 2026 (Audio Description)',
            'Sep 16, 2026 - 04:21 PM'),
        row('2', 'City Council Closed Special Session: September 8, 2026 (AUDIO ONLY)',
            'Sep 9, 2026 - 11:48 AM'),
        row('3', 'City Council Meeting: September 8, 2026',
            'Sep 8, 2026 - 06:00 PM'),
    )
    assert [m['event_id'] for m in meetings] == ['1', '2', '3']


def test_undated_titles_require_same_day_and_unambiguous_pair():
    meetings = parse(
        row('1', 'Council Meeting', 'Aug 24, 2026 - 06:00 PM'),
        row('2', 'Council Meeting (AUDIO DESCRIPTION)', 'Aug 24, 2026 - 11:00 PM'),
        row('3', 'Council Meeting (Audio Description)', 'Aug 27, 2026 - 08:00 AM'),
        row('4', 'Board Meeting', 'Aug 24, 2026 - 01:00 PM'),
        row('5', 'Board Meeting', 'Aug 24, 2026 - 06:00 PM'),
        row('6', 'Board Meeting (Audio Description)', 'Aug 24, 2026 - 11:00 PM'),
    )
    assert [m['event_id'] for m in meetings] == ['1', '3', '4', '5', '6']
