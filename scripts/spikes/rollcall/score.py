"""Score the deterministic parser against Legistar API ground truth.

Publish gate (the design under test): a passage is published only if
(1) every category's name count equals the stated count, and
(2) every name resolves uniquely against the roster gazetteer, and
(3) each canonical member appears exactly once across all categories.
Everything else is an abstention, never a guess.

Matching: GT voted items and parser passages are grouped by matter file and
aligned in order of appearance (n-th passage for file F <-> n-th GT item for F).
"""

import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import parse

ROOT = Path(__file__).parent
GT = json.loads((ROOT / "out" / "ground_truth.json").read_text())


def pdf_text(pdf_path: str) -> str:
    p = Path(pdf_path)
    txt = ROOT / "out" / (p.stem + ".txt")
    if not txt.exists():
        txt.parent.mkdir(parents=True, exist_ok=True)
        code = (
            "import fitz,sys;"
            "doc=fitz.open(sys.argv[1]);"
            "open(sys.argv[2],'w').write(''.join(pg.get_text() for pg in doc))"
        )
        subprocess.run(
            ["uv", "run", "--with", "pymupdf", "python", "-c", code, str(p), str(txt)],
            check=True,
        )
    return txt.read_text()


def norm_file(client: str, f: str | None) -> str | None:
    if not f:
        return None
    f = f.strip()
    if client == "milwaukee":
        # API gives e.g. "260488" already; keep digits only
        return "".join(c for c in f if c.isdigit())
    return f  # denver "26-0949"


overall = Counter()
taxonomy = Counter()
tax_examples = defaultdict(list)
gate = {"caught_bad": 0, "false_abstention": 0, "details": []}
report_rows = []

for client, events in GT.items():
    roster = sorted(
        {v["Person"] for ev in events for it in ev["Items"] for v in it["Votes"] if v["Person"]}
    )
    gz = parse.Gazetteer(roster)
    C = Counter()

    for ev in events:
        text = pdf_text(ev["PdfPath"])
        passages = parse.PARSERS[client](text)

        by_file_pass = defaultdict(list)
        for p in passages:
            if p.sections:
                by_file_pass[norm_file(client, p.matter_file)].append(p)

        gt_voted = [it for it in ev["Items"] if it["Votes"]]
        gt_matched = [it for it in gt_voted if it["MatterFile"]]
        gt_procedural = [it for it in gt_voted if not it["MatterFile"]]
        C["gt_voted_items"] += len(gt_voted)
        C["gt_procedural_nofile"] += len(gt_procedural)

        seen_idx = defaultdict(int)
        matched_sigs = set()
        for it in gt_matched:
            f = norm_file(client, it["MatterFile"])
            gt_tuples = {
                (v["Person"], parse.canon_value(v["Value"])) for v in it["Votes"]
            }

            cands = by_file_pass.get(f, [])
            idx = seen_idx[f]
            seen_idx[f] += 1
            if idx >= len(cands):
                # Legistar sometimes carries two identical EventItems for one
                # minutes motion (same file/action/votes); that is a GT-side
                # artifact, not a parse miss -- excluded from denominators
                sig = (f, it["ActionName"], frozenset(gt_tuples))
                if sig in matched_sigs:
                    C["gt_duplicate_eventitem"] += 1
                    taxonomy[f"{client}:gt_duplicate_eventitem_api_artifact"] += 1
                    tax_examples[f"{client}:gt_duplicate_eventitem_api_artifact"].append(
                        f"ev{ev['EventId']} {f} action={it['ActionName']} agenda={it['AgendaNumber']}"
                    )
                    continue
                C["gt_items"] += 1
                C["gt_tuples"] += len(gt_tuples)
                C["item_not_found"] += 1
                taxonomy[f"{client}:item_not_found_in_pdf"] += 1
                tax_examples[f"{client}:item_not_found_in_pdf"].append(
                    f"ev{ev['EventId']} {f} '{(it['Title'] or '')[:60]}' action={it['ActionName']}"
                )
                continue
            C["gt_items"] += 1
            C["gt_tuples"] += len(gt_tuples)
            matched_sigs.add((f, it["ActionName"], frozenset(gt_tuples)))
            p = cands[idx]

            decision = p.evaluate_publish_gate(gz)
            votes = decision.votes
            unresolved = decision.unresolved
            if not decision.publishable:
                C["gate_abstained"] += 1
                if not p.tally_ok:
                    taxonomy[f"{client}:gate_tally_mismatch"] += 1
                if unresolved:
                    taxonomy[f"{client}:gate_unresolved_name"] += 1
                if decision.duplicate_members:
                    taxonomy[f"{client}:gate_duplicate_member"] += 1
                if not p.sections or (not votes and not unresolved):
                    taxonomy[f"{client}:gate_empty_vote"] += 1
                # would publishing have been wrong?
                if (
                    set(votes) == gt_tuples
                    and not unresolved
                    and not decision.duplicate_members
                ):
                    gate["false_abstention"] += 1
                else:
                    gate["caught_bad"] += 1
                gate["details"].append(
                    f"{client} ev{ev['EventId']} {f}: {'; '.join(decision.reasons)}"
                )
                continue

            C["published"] += 1
            ext_tuples = set(votes)
            inter = ext_tuples & gt_tuples
            C["ext_tuples"] += len(ext_tuples)
            C["tuples_matched"] += len(inter)
            if ext_tuples != gt_tuples:
                C["items_with_tuple_diff"] += 1
                fp = sorted(ext_tuples - gt_tuples)
                fn = sorted(gt_tuples - ext_tuples)
                # classify the diff
                fp_names = {n for n, _ in fp}
                fn_names = {n for n, _ in fn}
                if fp_names == fn_names and fp_names:
                    taxonomy[f"{client}:vote_value_mismatch"] += 1
                elif not fp and fn:
                    taxonomy[f"{client}:gt_extra_members"] += 1
                elif fp and not fn:
                    taxonomy[f"{client}:parser_extra_members"] += 1
                else:
                    taxonomy[f"{client}:member_set_mismatch"] += 1
                tax_examples[f"{client}:tuple_diff"].append(
                    f"ev{ev['EventId']} {f} fp={fp[:4]} fn={fn[:4]}"
                )

            gt_pass = it["PassedFlag"]
            if gt_pass is not None and p.outcome is not None:
                C["outcome_compared"] += 1
                if (p.outcome == "PASS") == bool(gt_pass):
                    C["outcome_correct"] += 1
                else:
                    taxonomy[f"{client}:outcome_mismatch"] += 1
                    tax_examples[f"{client}:outcome_mismatch"].append(
                        f"ev{ev['EventId']} {f} parser={p.outcome} api_flag={gt_pass} "
                        f"action={it['ActionName']} motion='{p.motion_text[:80]}'"
                    )

        # passages the parser published for files the API has no votes for
        gt_files = Counter(norm_file(client, it["MatterFile"]) for it in gt_matched)
        for f, ps in by_file_pass.items():
            extra = len(ps) - gt_files.get(f, 0)
            if extra > 0:
                C["passages_without_gt"] += extra

    prec = C["tuples_matched"] / C["ext_tuples"] if C["ext_tuples"] else 0
    rec = C["tuples_matched"] / C["gt_tuples"] if C["gt_tuples"] else 0
    cov = C["published"] / C["gt_items"] if C["gt_items"] else 0
    out = C["outcome_correct"] / C["outcome_compared"] if C["outcome_compared"] else 0
    report_rows.append((client, C, prec, rec, cov, out))
    overall.update(C)

print("=" * 78)
for client, C, prec, rec, cov, out in report_rows:
    print(f"\n[{client}]  gt items w/ votes+file: {C['gt_items']}  "
          f"(+{C['gt_procedural_nofile']} procedural no-file)")
    print(f"  coverage (published/gt): {C['published']}/{C['gt_items']} = {cov:.1%}")
    print(f"  per-member precision:    {C['tuples_matched']}/{C['ext_tuples']} = {prec:.1%}")
    print(f"  per-member recall:       {C['tuples_matched']}/{C['gt_tuples']} = {rec:.1%}")
    print(f"  outcome accuracy:        {C['outcome_correct']}/{C['outcome_compared']} = {out:.1%}")
    print(f"  gate abstentions: {C['gate_abstained']}  not-found: {C['item_not_found']}  "
          f"passages w/o GT votes: {C['passages_without_gt']}")

C = overall
prec = C["tuples_matched"] / C["ext_tuples"] if C["ext_tuples"] else 0
rec = C["tuples_matched"] / C["gt_tuples"] if C["gt_tuples"] else 0
cov = C["published"] / C["gt_items"] if C["gt_items"] else 0
out = C["outcome_correct"] / C["outcome_compared"] if C["outcome_compared"] else 0
print(f"\n[OVERALL] items {C['gt_items']}  coverage {cov:.1%}  precision {prec:.1%}  "
      f"recall {rec:.1%}  outcome acc {out:.1%}")

print("\n-- publish-gate verdict --")
print(f"  abstentions that caught a real problem: {gate['caught_bad']}")
print(f"  false abstentions (data was correct):    {gate['false_abstention']}")
for d in gate["details"][:20]:
    print("   ", d)

print("\n-- failure taxonomy --")
for k, v in taxonomy.most_common():
    print(f"  {v:3d}  {k}")
print()
for k, exs in tax_examples.items():
    print(f"  [{k}]")
    for e in exs[:8]:
        print(f"     {e}")

score_report = {
    "rows": [
        {"client": c, "counts": dict(C), "precision": p, "recall": r,
         "coverage": cv, "outcome_acc": o}
        for c, C, p, r, cv, o in report_rows
    ],
    "taxonomy": dict(taxonomy),
    "gate": {k: v for k, v in gate.items() if k != "details"},
    "gate_details": gate["details"],
    "examples": {k: v[:20] for k, v in tax_examples.items()},
}
(ROOT / "scores.json").write_text(json.dumps(score_report, indent=1) + "\n")
