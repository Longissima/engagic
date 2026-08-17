"""Item filtering - store everything, filter at processing time"""

import hashlib
import re
from dataclasses import dataclass
from typing import Optional, Sequence


ITEM_FILTER_VERSION = "ifv1"
ATTACHMENT_FILTER_VERSION = "afv1"


@dataclass(frozen=True, slots=True)
class FilterDecision:
    reason: str
    rule_id: str
    version: str


def _rule_id(namespace: str, pattern: str) -> str:
    digest = hashlib.sha256(pattern.encode("utf-8")).hexdigest()[:16]
    return f"{namespace}:{digest}"


def _match_decision(
    value: str,
    reason: str,
    patterns: Sequence[str],
    *,
    version: str,
) -> Optional[FilterDecision]:
    for pattern in patterns:
        if re.search(pattern, value, re.IGNORECASE):
            return FilterDecision(
                reason=reason,
                rule_id=_rule_id(reason, pattern),
                version=version,
            )
    return None


def system_filter_decision(reason: str) -> FilterDecision:
    return FilterDecision(
        reason=reason,
        rule_id=f"system:{reason}",
        version=ITEM_FILTER_VERSION,
    )

# Meeting level: test/demo meetings to skip entirely
MEETING_SKIP_PATTERNS = [
    r'\bmock\b',  # "Mock Select Committee", "Mock Hearing"
    r'\btest\b',  # "Test Meeting", "Test Committee"
    r'\bdemo\b',  # "Demo Session"
    r'\btraining\b',  # "Training Session"
    r'\bpractice\b',  # "Practice Meeting"
]

# --- Processing skip patterns (organized by category) ---

# Procedural — zero substantive content (formerly ADAPTER_SKIP_PATTERNS)
PROCEDURAL_PATTERNS = [
    r'roll call',
    r'invocation',
    r'pledge of allegiance',
    r'flag salute',  # Regional variant of pledge
    r'approval of\s+(?:the\s+|draft\s+)?(?:meeting\s+)?(?:minutes|agenda)\b',  # "Approval of Draft Minutes", "Approval of Agenda"
    r'\bapprove\s+(?:the\s+|draft\s+)?(?:meeting\s+)?minutes\b',  # "Approve the minutes of..." -- requires "minutes" right after the verb, not "Approve and record into the minutes [tax refunds]"
    r'adopt minutes',
    r'review of minutes',
    r'^minutes of',  # Standalone minutes items
    r'draft.*minutes',  # Draft minutes items
    r'adjourn',
    r'call to order',
    r'\brecess\b',  # Meeting recess (\b prevents matching "recession")
    r'moment of silence',
    r'public comment',  # The period itself, not the content
    r'^communications?\s*$',  # Standalone "Communications" agenda section -- not "Telecommunications Agreement"
    r'communications? from\s+the\s+public',  # Public comment routing; substantive senders (mayor, manager, council, staff, board) fall through to summarization
    r'time fixed for next',
    r'identify items (to|for)',  # Future items placeholder ("identify items to discuss")
    r'meeting schedule for',  # Calendar scheduling items
    r'^approve bills\.?\s*$',  # County boilerplate ("Approve Bills.") -- \s*$ handles the trailing space from f"{title} {item_type}"
    r'^ratify\b.*\b(disburse|payment|warrant)',  # "Ratify the release of time-sensitive disbursements..."
    r'^(authorize|authorization)\s+(?:advertising|advertisement)\s+for\s+(bids?|proposals)',  # Procurement notices, no policy substance until bids return
]

# Ceremonial — searchable names, no policy substance
CEREMONIAL_PATTERNS = [
    r'\bproclamations?(?!.{0,30}\b(emergency|disaster|local emergency|state of emergency))',
    r'\bcommendation\b',
    r'\brecognition\b',
    r'\bceremonial\b',
    r'\bbenediction\b',
    r'\boath of office\b',
    r'\bswearing[ -]in\b',
    r'\bin memoriam\b',
    r'\bopening remarks?\b',
    r'(?i)congratulations (to|extended to|for)',
    r'(?i)tribute to (late|the late)',
    r'(?i)\bon (his|her|their) retirement\b',
    r'(?i)retirement of',
    r'(?i)happy birthday',
    r'(?i)birthday (wishes|greetings|recognition|celebration)',
]

# Administrative — save for record, no LLM value
ADMINISTRATIVE_PATTERNS = [
    r'\bappointment\b',
    r'\bconfirmation of\b.*\b(appointment|appointee|nominee|hiring|director|chief|officer|commissioner)',  # Tightened from \bconfirmation\b which caught "effective upon confirmation from..."
    r'(?i)liquor license',
    r'(?i)beer (and|&) wine license',
    r'(?i)alcoholic beverage license',
    r'issuance of permits? for sign',
    r'signboard permit',
    r'fee waiver for',
    r'(various )?small claims?',
]

# Combined for the single skip check
PROCESSOR_SKIP_PATTERNS = PROCEDURAL_PATTERNS + CEREMONIAL_PATTERNS + ADMINISTRATIVE_PATTERNS

# High token cost, low value (scanned form letters, bulk documents)
PUBLIC_COMMENT_PATTERNS = [
    r'public comment',
    r'public correspondence',
    r'comment letter',
    r'comment ltrs',  # SF abbreviation
    r'written comment',
    r'public hearing comment',
    r'citizen comment',
    r'correspondence received',
    r'public input',
    r'public testimony',
    r'letters received',
    r'petitions',  # SF "Petitions and Communications"
    r'pub corr',  # SF abbreviation
    r'pulbic corr',  # Common typo in SF data
    r'comm pkt',  # Committee packets
    r'cmte pkt',  # Committee packets (alternate abbreviation)
    r'committee packet',
    r'board pkt',  # Board packets (SF compilation format)
    r'co-?sponsor(ship)?\s*(request|ltr|letter)',  # "Co-Sponsor Request Chen 122525"
    r'sponsor(ship)?\s*request',
    r'communications?\s+from\s+public',  # LA: "Communication(s) from Public_03-18-2026"
    r'community impact statement',  # LA: neighborhood council public comment
    r'speaker card',  # LA: "Speaker Card(s)_02-24-2026"
]

# Massive PDFs with no policy content (property lists, parcel tables)
PARCEL_TABLE_PATTERNS = [
    r'parcel table',
    r'parcel list',
    r'parcel map',
    r'tax parcel',
    r'property list',
    r'property table',
    r'assessor(?:.s)?\s+(?:parcel\s+)?(?:table|list|report|roll|data)',
    r'apn list',  # Assessor Parcel Number
    r'parcel number',
]

# Boilerplate vendor/contract documents (huge, not substantive to decision)
BOILERPLATE_CONTRACT_PATTERNS = [
    r'omnia partners contract',  # Cooperative purchasing agreements (100s of pages each)
    r'sourcewell contract',  # Another cooperative purchasing org
    r'naspo valuepoint',  # State purchasing cooperative
    r'u\.?s\.? communities',  # US Communities contracts
    r'hgac.?buy',  # Houston-Galveston Area Council cooperative
    r'master agreement',  # Generic master agreements (often boilerplate)
    r'terms and conditions',  # T&C documents
    r'general conditions',  # Construction general conditions
    r'insurance certificate',  # COI documents
    r'certificate of insurance',
    r'w-?9',  # Tax forms
    r'bid tabulation',  # Bid results tables (useful but huge)
    r'^budget transfer form$',  # Fort Bend etc. -- the item title already says "transfer $X from Y to Z for purpose"; the form just restates it as a checkbox sheet
    r"^director'?s?\s+form$",  # Same family: salary/personnel transfer cover form, no policy substance beyond the title
]

# SF procedural boilerplate (internal routing, compliance checkboxes, no policy substance)
# The actual legislative content is in "Leg Ver*" and "PC Transmittal"
SF_PROCEDURAL_PATTERNS = [
    r'ceqa det',  # CEQA Determination - checkbox form saying "categorically exempt"
    r'ceqa determination',
    r'referral ceqa',  # Referral to CEQA review - internal routing
    r'referral fyi',  # FYI referral - just routing notification
    r'myr memo',  # Mayor's memo - cover letter, no substance
    r'mayor.?s? memo',
    r'comm rpt rqst',  # Committee Report Request Memo - internal request
    r'committee report request',
    r'referral.*pc\b',  # Referral to Planning Commission - routing form
    r'hearing notice',  # Notice that hearing will occur - no content
    r'notice of (?:public )?hearing',  # "Notice of Public Hearing" - same thing, different phrasing
]

# Environmental Impact Reports - massive technical documents (200-500+ pages)
# Important for environmental review but too large for LLM summarization
EIR_PATTERNS = [
    r'\bfeir\b',  # Final EIR
    r'\bdeir\b',  # Draft EIR
    r'\bseir\b',  # Supplemental EIR
    r'\beir\b',   # Generic EIR reference
    r'environmental impact report',
    r'ceqa findings',  # CEQA findings document (different from CEQA Det form)
    r'initial study',  # CEQA initial study
    r'negative declaration',  # Mitigated Negative Declaration
    r'notice of preparation',  # NOP for EIR
]

# Administrative matter types (not legislative)
# Note: uses substring matching - removed 'Information' and 'Inf' as they match 'Informational Report'
SKIP_MATTER_TYPES = [
    'Minutes (Min)',
    'Introduction & Referral Calendar (IRC)',
    'Information Item (Inf)',
    'Information Item',
    'Information Only',
    # City-specific variants
    'Minutes',
    'Min',
    'IRC',
    'Referral Calendar',
]


def should_skip_meeting(title: str) -> bool:
    """Meeting level: should entire meeting be skipped (test/demo/mock)?"""
    return any(re.search(p, title, re.IGNORECASE) for p in MEETING_SKIP_PATTERNS)


def get_filter_decision(
    title: str, item_type: str = ""
) -> Optional[FilterDecision]:
    combined = f"{title} {item_type}".lower()
    for reason, patterns in (
        ("procedural", PROCEDURAL_PATTERNS),
        ("ceremonial", CEREMONIAL_PATTERNS),
        ("administrative", ADMINISTRATIVE_PATTERNS),
    ):
        decision = _match_decision(
            combined,
            reason,
            patterns,
            version=ITEM_FILTER_VERSION,
        )
        if decision:
            return decision
    return None


def get_skip_reason(title: str, item_type: str = "") -> Optional[str]:
    """Get the skip reason category for an item, or None if it should be processed.

    Returns: "procedural", "ceremonial", "administrative", or None.
    """
    decision = get_filter_decision(title, item_type)
    return decision.reason if decision else None


def should_skip_processing(title: str, item_type: str = "") -> bool:
    """Processor level: should item skip LLM processing (but still be saved)?"""
    return get_skip_reason(title, item_type) is not None


def should_skip_matter(matter_type: str) -> bool:
    """Should matter be skipped based on type (administrative/procedural)?"""
    if not matter_type:
        return False
    matter_lower = matter_type.lower()
    return any(skip.lower() in matter_lower for skip in SKIP_MATTER_TYPES)


def get_attachment_filter_decision(name: str) -> Optional[FilterDecision]:
    name_lower = name.lower()
    for reason, patterns in (
        ("public_comment", PUBLIC_COMMENT_PATTERNS),
        ("parcel_table", PARCEL_TABLE_PATTERNS),
        ("boilerplate_contract", BOILERPLATE_CONTRACT_PATTERNS),
        ("sf_procedural", SF_PROCEDURAL_PATTERNS),
        ("environmental_report", EIR_PATTERNS),
    ):
        decision = _match_decision(
            name_lower,
            reason,
            patterns,
            version=ATTACHMENT_FILTER_VERSION,
        )
        if decision:
            return decision
    return None


def is_public_comment_attachment(name: str) -> bool:
    """Whether an attachment is excluded from summary-input change detection."""
    return get_attachment_filter_decision(name) is not None
