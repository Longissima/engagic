from vendors.adapters.parsers.granicus_parser import parse_viewpublisher_listing


def test_responsive_listing_keeps_dates_documents_and_deduplicates():
    html = '''
    <ol class="responsive-table">
      <li class="table-row table-row--head"><div class="table-cell">Name</div></li>
      <li class="table-row">
        <div class="table-cell archive-name">Board &amp; Committees</div>
        <div class="table-cell archive-date">Sep 16, 2026</div>
        <div class="table-cell archive-agenda"><a href="//example.granicus.com/AgendaViewer.php?view_id=5&amp;clip_id=2822">Agenda</a></div>
        <div class="table-cell archive-minutes"><a href="/minutes/2822.pdf">Approved Minutes</a></div>
      </li>
      <li class="table-row">
        <div class="table-cell">Future meeting without agenda</div>
        <div class="table-cell">Sep 30, 2026</div>
      </li>
    </ol>
    <table><tr class="odd">
      <td class="listItem">Board &amp; Committees</td>
      <td class="listItem">Sep 16, 2026</td>
      <td class="listItem"><a href="//example.granicus.com/AgendaViewer.php?view_id=5&amp;clip_id=2822">Agenda</a></td>
    </tr></table>
    '''
    rows = parse_viewpublisher_listing(html, 'https://example.granicus.com')
    assert len(rows) == 1
    assert rows[0]['event_id'] == '2822'
    assert rows[0]['title'] == 'Board & Committees'
    assert rows[0]['start'] == '2026-09-16T00:00:00'
    assert rows[0]['agenda_viewer_url'].startswith('https://example.granicus.com/AgendaViewer')
    assert rows[0]['minutes_url'] == 'https://example.granicus.com/minutes/2822.pdf'
