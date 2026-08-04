"""Deterministic roll-call parser for Legistar-published minutes PDFs.

Zero LLM calls: regex + gazetteer + arithmetic. Two template drivers:

  milwaukee  "A motion was made by ALD. X that this Y be ADOPTED.  This motion
              PREVAILED by the following vote:" followed by category blocks
              ("Aye," names... "15 - " / "No," "0").

  denver     "A motion offered by Council member X, duly seconded by ..., that
              Council Bill 26-0864 ... carried by the following vote:" followed
              by "Aye:" names "(12)" / "Nay:" "(None) (0)" / "Absent:" ...

The publish gate: a passage is publishable only if every category's extracted
name count equals its stated count. Anything else is ABSTAIN (from publishing),
never a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- vote values

CANON = {
    "aye": "AYE", "yea": "AYE", "yes": "AYE", "y": "AYE",
    "no": "NO", "nay": "NO", "n": "NO",
    "abstain": "ABSTAIN", "abstention": "ABSTAIN", "abstained": "ABSTAIN",
    "excused": "EXCUSED",
    "absent": "ABSENT",
    "recused": "RECUSED", "conflict": "RECUSED",
    "present": "PRESENT",
    "non-voting": "NONVOTING", "non voting": "NONVOTING",
}


def canon_value(v: str) -> str:
    return CANON.get(v.strip().lower().rstrip(".,:"), v.strip().upper())


# ------------------------------------------------------------------ gazetteer

_TITLE = re.compile(
    r"^\s*(ald\.?|alderman|alderwoman|council\s*member|councilmember|"
    r"council\s+president(\s+pro[- ]tem)?|council\s+pro[- ]tem|president|"
    r"mr\.?|ms\.?|mrs\.?|dr\.?)\s+",
    re.I,
)
_SUFFIX = re.compile(r"\s*,?\s*(jr\.?|sr\.?|ii|iii|iv)\s*$", re.I)


def _clean(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" ,. ")
    prev = None
    while prev != name:
        prev = name
        name = _TITLE.sub("", name).strip(" ,")
    return name


class Gazetteer:
    """Roster lookup: raw minutes name -> canonical roster name.

    Keys per roster member: full name, surname, surname+suffix, last two words
    (multi-word surnames like 'Romero Campbell'). Ambiguous keys are dropped.
    """

    def __init__(self, roster: list[str]):
        self.roster = roster
        keys: dict[str, set[str]] = {}

        def add(k: str, full: str):
            k = re.sub(r"\s+", " ", k).strip().lower().replace("’", "'")
            if k:
                keys.setdefault(k, set()).add(full)

        for full in roster:
            base = _clean(full)
            add(base, full)
            nosuf = _SUFFIX.sub("", base)
            words = nosuf.split()
            if words:
                add(words[-1], full)                       # surname
                if len(words) >= 2:
                    add(" ".join(words[-2:]), full)        # two-word surname
                if nosuf != base:                          # surname + suffix
                    suf = base[len(nosuf):].strip(" ,")
                    add(f"{words[-1]} {suf}", full)
                    add(f"{words[-1]}, {suf}", full)
        self.keys = {k: next(iter(v)) for k, v in keys.items() if len(v) == 1}
        self.ambiguous = {k for k, v in keys.items() if len(v) > 1}

    def resolve(self, raw: str) -> str | None:
        k = _clean(raw).lower().replace("’", "'")
        k = re.sub(r"\s+", " ", k)
        if k in self.keys:
            return self.keys[k]
        k2 = _SUFFIX.sub("", k)
        if k2 != k and k2 in self.keys:
            return self.keys[k2]
        return None


# -------------------------------------------------------------------- results

@dataclass
class Passage:
    matter_file: str | None
    motion_text: str
    action: str | None            # ADOPTED / PASSED / postponed... (raw)
    outcome: str | None           # PASS / FAIL from carried|PREVAILED|FAILED
    sections: list[tuple[str, list[str], int]] = field(default_factory=list)
    # (canonical value, [raw names], stated count)
    tally_ok: bool = True
    tally_notes: list[str] = field(default_factory=list)

    def votes(self, gz: Gazetteer):
        out, unresolved = [], []
        for value, names, _ in self.sections:
            for raw in names:
                full = gz.resolve(raw)
                if full is None:
                    unresolved.append((raw, value))
                else:
                    out.append((full, value))
        return out, unresolved

    def validate(self):
        self.tally_notes = []
        for value, names, stated in self.sections:
            if len(names) != stated:
                self.tally_notes.append(
                    f"{value}: extracted {len(names)} names, stated {stated}"
                )
        self.tally_ok = not self.tally_notes


# ---------------------------------------------------------------- page strip

MKE_FURNITURE = [
    re.compile(r"^Page \d+\s*$"),
    re.compile(r"^City of Milwaukee\s*$"),
    re.compile(r"^COMMON COUNCIL\s*$"),
    re.compile(r"^Meeting Minutes\s*$"),
    re.compile(r"^[A-Z][a-z]+ \d{1,2}, \d{4}\s*$"),  # header date line
]
DEN_FURNITURE = [re.compile(r"^Page \d+\s*$")]


def strip_furniture(text: str, pats) -> list[str]:
    return [
        ln for ln in text.splitlines()
        if ln.strip() and not any(p.match(ln.strip()) for p in pats)
    ]


# ------------------------------------------------------------------ milwaukee

MKE_MOTION = re.compile(
    r"A motion was made by\s+(?P<mover>[A-Z][A-Za-z.'\- ]+?)\s+that\s+"
    r"(?:this|these)\b.*?\bbe\s+(?P<action>[A-Z][A-Za-z /,'&\-]+?)\s*\.\s+"
    r"This motion\s+(?P<result>PREVAILED|FAILED)\s+by the following vote:",
    re.S,
)
# passive disposition with a bare vote list and no motion sentence:
# "This Resolution was  PLACED ON FILE" / "Aye," ...
MKE_THIS_WAS = re.compile(
    r"This\s+\w+\s+was\s+(?P<action>[A-Z][A-Z /,'&\-]+?)\s+"
    r"(?=(?:Aye|No|Nay|Excused|Abstain|Absent|Present|Recused),)"
)
MKE_THIS_WAS_LINE = re.compile(r"^This\s+\w+\s+was\s+[A-Z][A-Z /,'&\-]*\s*$")
MKE_CATEGORY = re.compile(r"^(Aye|No|Nay|Abstain|Excused|Absent|Present|Recused),?\s*$")
MKE_COUNT = re.compile(r"^(\d+)\s*(?:-\s*)?$")
MKE_NAME_SPLIT = re.compile(r",?\s*(?=Ald\s*\.)")


def _mke_names(blob: str) -> list[str]:
    parts = [p for p in MKE_NAME_SPLIT.split(blob) if p.strip(" ,")]
    # "Ald.Pratt" carries no space after the title, so strip it explicitly
    parts = [re.sub(r"^\s*Ald\s*\.\s*", "", p) for p in parts]
    return [_clean(p) for p in parts if _clean(p)]


def parse_milwaukee(text: str) -> list[Passage]:
    lines = strip_furniture(text, MKE_FURNITURE)
    # anchors: standalone 6-digit file numbers (agenda "N." line usually precedes)
    anchors = [i for i, ln in enumerate(lines) if re.fullmatch(r"\d{6}", ln.strip())]
    passages: list[Passage] = []
    bounds = anchors + [len(lines)]
    for a, b in zip(anchors, bounds[1:]):
        file_no = lines[a].strip()
        block = lines[a:b]
        joined = " ".join(ln.strip() for ln in block)
        motions = sorted(
            list(MKE_MOTION.finditer(joined)) + list(MKE_THIS_WAS.finditer(joined)),
            key=lambda m: m.start(),
        )

        # vote runs in line order; a run opens at each "... following vote:"
        # or at a passive disposition line
        sections_runs: list[list[tuple[str, list[str], int]]] = []
        cur: list[tuple[str, list[str], int]] | None = None
        i = 0
        while i < len(block):
            ln = block[i].strip()
            if ln.endswith("vote:") or MKE_THIS_WAS_LINE.match(ln):
                if cur is not None:
                    sections_runs.append(cur)
                cur = []
                i += 1
                continue
            cm = MKE_CATEGORY.match(ln)
            if cm and cur is not None:
                value = canon_value(cm.group(1))
                names_blob, stated = [], None
                j = i + 1
                while j < len(block):
                    nx = block[j].strip()
                    cnt = MKE_COUNT.match(nx)
                    if cnt:
                        stated = int(cnt.group(1))
                        break
                    if MKE_CATEGORY.match(nx) or nx.endswith("vote:"):
                        break
                    names_blob.append(nx)
                    j += 1
                if stated is not None:
                    names = _mke_names(" ".join(names_blob)) if stated > 0 else []
                    cur.append((value, names, stated))
                    i = j + 1
                    continue
            i += 1
        if cur is not None:
            sections_runs.append(cur)

        for k, m in enumerate(motions):
            gd = m.groupdict()
            # passive "This X was ADOPTED" states a completed action: PASS
            result = gd.get("result") or "PREVAILED"
            p = Passage(
                matter_file=file_no,
                motion_text=re.sub(r"\s+", " ", m.group(0))[:300],
                action=gd["action"].strip(),
                outcome="PASS" if result == "PREVAILED" else "FAIL",
            )
            if k < len(sections_runs):
                p.sections = sections_runs[k]
            passages.append(p)
    for p in passages:
        p.validate()
    return passages


# --------------------------------------------------------------------- denver

DEN_MOTION = re.compile(
    r"A motion offered by\s+(?P<mover>.+?),\s+duly seconded by\s+"
    r"(?P<seconder>.+?),\s+(?:that|to)\s+(?P<what>.+?),\s+"
    r"(?P<result>carried|failed)\s+by the\s*following\s*vote:",
    re.S,
)
# amendment variant: the vote sentence is detached from the motion sentence --
# "... be amended in the following particulars: \"...\" The motion to amend
# carried by the following vote:"
DEN_MOTION2 = re.compile(
    r"A motion offered by\s+(?P<mover>.+?),\s+duly seconded by\s+"
    r"(?P<seconder>.+?),\s+(?:that|to)\s+(?P<what>.+?)\s+in the following\s+"
    r"particulars:.*?The motion to \w+\s+(?P<result>carried|failed)\s+"
    r"by the\s*following\s*vote:",
    re.S,
)
DEN_CATEGORY = re.compile(r"^(Aye|Nay|No|Abstain|Excused|Absent|Present|Recused):\s*$")
DEN_TERMINAL = re.compile(r"\((\d+)\)\s*$")
DEN_FILE = re.compile(r"^(\d{2}-\d{4})\b")
DEN_FILE_IN_MOTION = re.compile(r"\b(\d{2}-\d{4})\b")


def parse_denver(text: str) -> list[Passage]:
    lines = strip_furniture(text, DEN_FURNITURE)
    anchors = [0] + [
        i for i, ln in enumerate(lines)
        if DEN_FILE.match(ln.strip()) or ln.strip() == "Block Vote"
    ]
    passages: list[Passage] = []
    bounds = sorted(set(anchors)) + [len(lines)]
    for a, b in zip(bounds, bounds[1:]):
        head = lines[a].strip()
        fm = DEN_FILE.match(head)
        file_no = fm.group(1) if fm else None
        block = lines[a:b]
        joined = " ".join(ln.strip() for ln in block)
        # both patterns end at a unique "...vote:" anchor; dedupe on end pos,
        # preferring the tighter (later-starting) match
        by_end: dict[int, re.Match] = {}
        for m in list(DEN_MOTION.finditer(joined)) + list(DEN_MOTION2.finditer(joined)):
            prev = by_end.get(m.end())
            if prev is None or m.start() > prev.start():
                by_end[m.end()] = m
        motions = [by_end[e] for e in sorted(by_end)]
        # vote sections in line order
        sections_runs: list[list[tuple[str, list[str], int]]] = []
        cur: list[tuple[str, list[str], int]] = []
        i = 0
        while i < len(block):
            ln = block[i].strip()
            if "by the following vote:" in ln or ln.endswith("vote:"):
                if cur:
                    sections_runs.append(cur)
                cur = []
                # action/result line follows immediately ("Adopted", ...)
                i += 1
                continue
            cm = DEN_CATEGORY.match(ln)
            if cm:
                value = canon_value(cm.group(1))
                blob_lines, stated = [], None
                j = i + 1
                while j < len(block):
                    nx = block[j].strip()
                    t = DEN_TERMINAL.search(nx)
                    blob_lines.append(nx)
                    if t:
                        stated = int(t.group(1))
                        break
                    if DEN_CATEGORY.match(nx):
                        blob_lines.pop()
                        break
                    j += 1
                if stated is not None:
                    blob = DEN_TERMINAL.sub("", " ".join(blob_lines)).strip()
                    if blob.lower() in ("(none)", "none", ""):
                        names = []
                    else:
                        names = [
                            _clean(x) for x in blob.split(",") if _clean(x)
                        ]
                    cur.append((value, names, stated))
                    i = j + 1
                    continue
            i += 1
        if cur:
            sections_runs.append(cur)

        for k, m in enumerate(motions):
            mentioned = DEN_FILE_IN_MOTION.findall(m.group("what"))
            attributed = file_no
            if mentioned and file_no and file_no not in mentioned:
                attributed = mentioned[0]
            if mentioned and not file_no:
                attributed = mentioned[0]
            p = Passage(
                matter_file=attributed,
                motion_text=re.sub(r"\s+", " ", m.group(0))[:300],
                action=None,
                outcome="PASS" if m.group("result") == "carried" else "FAIL",
            )
            if k < len(sections_runs):
                p.sections = sections_runs[k]
            passages.append(p)
    for p in passages:
        p.validate()
    return passages


PARSERS = {"milwaukee": parse_milwaukee, "denver": parse_denver}
