"""Explicit jurisdiction/body and document context for every summary lane."""


def civic_context(jurisdiction, meeting, committee=None):
    name = getattr(jurisdiction, 'name', None) or 'Not recorded'
    state = getattr(jurisdiction, 'state', None) or ''
    kind = getattr(jurisdiction, 'type', None) or 'Not recorded'
    kind = str(getattr(kind, 'value', kind)).replace('_', ' ')
    title = getattr(meeting, 'title', None) or 'Not recorded'
    body = getattr(committee, 'name', None) or title
    date = getattr(meeting, 'date', None)
    return (
        '[APPLICATION CONTEXT]\n'
        f'Jurisdiction: {name}' + (f', {state}' if state else '') + '\n'
        f'Jurisdiction type: {kind}\n'
        f'Meeting body: {body}\nMeeting title: {title}\nMeeting date: {date or "Not recorded"}\n'
        'Use this body for the current item; distinguish other bodies and historical actions '
        'mentioned in supporting documents. Do not infer city council from jurisdiction type.\n'
        '[/APPLICATION CONTEXT]'
    )


def attachment_inventory(attachments):
    names = [getattr(a, 'name', None) or 'Unnamed attachment' for a in (attachments or [])]
    return (
        '[ATTACHMENT INVENTORY]\n'
        f'This item has {len(names)} linked attachment(s): ' + ('; '.join(names) or 'none') + '.\n'
        'A staff report is an attachment. "Attachments: none" inside a report means no '
        'additional supporting documents, not that this item has no attachments. '
        'Distinguish unavailable documents from readable documents that omit particular details.\n'
        '[/ATTACHMENT INVENTORY]'
    )
