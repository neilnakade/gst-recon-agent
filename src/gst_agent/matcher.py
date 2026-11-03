"""Reconcile your books (extracted invoices) against the portal data (GSTR-2B).

Plain Python rules, no AI. For every invoice in the books we decide one status:
  matched                 same invoice on both sides, amounts agree
  amount_mismatch         same invoice on both sides, but amounts differ
  invoice_no_format_diff  same invoice, but the number is written differently
                          (e.g. INV/2026/0005 vs INV-2026-0005)
  missing_in_2b           in your books, not on the portal
And separately: extra_in_2b = on the portal, but not in your books.

Run from the repo root (after the extractor has processed all invoices):
    python -m src.gst_agent.matcher
"""
import argparse
import json
import re
from pathlib import Path

from . import gstr2b, validate

TOLERANCE = 1.00  # rupees; ignore tiny rounding differences
AMOUNT_FIELDS = ["taxable_value", "cgst", "sgst", "igst"]


def normalize_no(invoice_no: str) -> str:
    """'INV/2026/0005' and 'inv-2026-0005' both become 'INV20260005'."""
    return re.sub(r"[^A-Z0-9]", "", invoice_no.upper())


def make_key(record: dict) -> tuple[str, str]:
    return (record["supplier_gstin"].strip().upper(), normalize_no(record["invoice_no"]))


def amount_differences(book: dict, portal: dict) -> dict:
    diffs = {}
    for f in AMOUNT_FIELDS:
        if abs(float(book[f]) - float(portal[f])) > TOLERANCE:
            diffs[f] = {"books": book[f], "portal": portal[f]}
    return diffs


def reconcile(books: list[dict], portal: list[dict]) -> dict:
    portal_by_key = {make_key(p): p for p in portal}
    used_keys = set()
    results = []

    for book in books:
        key = make_key(book)
        match = portal_by_key.get(key)
        row = {"invoice_no": book["invoice_no"], "supplier_gstin": book["supplier_gstin"],
               "supplier_name": book["supplier_name"], "details": {}}
        if match is None:
            row["status"] = "missing_in_2b"
        else:
            used_keys.add(key)
            diffs = amount_differences(book, match)
            if diffs:
                row["status"] = "amount_mismatch"
                row["details"] = diffs
            elif book["invoice_no"] != match["invoice_no"]:
                row["status"] = "invoice_no_format_diff"
                row["details"] = {"books": book["invoice_no"], "portal": match["invoice_no"]}
            else:
                row["status"] = "matched"
        results.append(row)

    extra_in_2b = [p for k, p in portal_by_key.items() if k not in used_keys]
    return {"results": results, "extra_in_2b": extra_in_2b}


def load_books(folder: Path) -> list[dict]:
    books = []
    for p in sorted(folder.glob("*.json")):
        book = json.loads(p.read_text())
        book["_source"] = p.name  # remember which file it came from (used in the review list)
        books.append(book)
    return books


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extracted", default="data/extracted")
    ap.add_argument("--portal", default="data/synthetic/gstr2b.json")
    ap.add_argument("--truth", default="data/synthetic/ground_truth.json")
    ap.add_argument("--out", default="data/recon_result.json")
    ap.add_argument("--buyer-gstin", default=None,
                    help="your own GSTIN, so invoices where it was read as the supplier get flagged")
    args = ap.parse_args()

    all_books = load_books(Path(args.extracted))
    portal, portal_info = gstr2b.load_portal(Path(args.portal))  # our simple list OR the official GSTR-2B structure
    buyer_gstin = args.buyer_gstin or portal_info.get("buyer_gstin") or None  # an official file knows your GSTIN
    books, review = validate.split_books(all_books, buyer_gstin)  # doubtful ones are set aside
    if portal_info:
        skipped = portal_info.get("skipped") or {}
        print(f"\nRead official GSTR-2B: period {portal_info.get('period') or '?'}, "
              f"{portal_info['invoices']} invoices; skipped (not matched): {skipped or 'nothing'}")
    outcome = reconcile(books, portal)
    outcome["needs_review"] = review
    Path(args.out).write_text(json.dumps(outcome, indent=2))

    counts = {}
    for r in outcome["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\nBooks: {len(all_books)} invoices ({len(review)} set aside for review)   Portal: {len(portal)} rows\n")
    for status, n in sorted(counts.items()):
        print(f"  {status:<24}{n}")
    print(f"  {'extra_in_2b':<24}{len(outcome['extra_in_2b'])}")
    print(f"  {'needs_review':<24}{len(review)}")
    for item in review:
        print(f"    {item['source'] or item['invoice_no']}: {item['reasons'][0]}")

    # Self-check against the answer key (only possible with synthetic data)
    truth_path = Path(args.truth)
    if truth_path.exists():
        truth = json.loads(truth_path.read_text())
        expected = truth["expected_status"]
        wrong = [(r["invoice_no"], expected.get(r["invoice_no"]), r["status"])
                 for r in outcome["results"] if expected.get(r["invoice_no"]) != r["status"]]
        total = len(outcome["results"])
        if review:  # a set-aside invoice's portal row would wrongly show up as "extra", so skip that check
            print(f"\nChecked against answer key: {total - len(wrong)}/{total} matched invoices have the "
                  "right status (extra-in-2B check skipped while invoices are set aside)")
        else:
            found_extra = {p["invoice_no"] for p in outcome["extra_in_2b"]}
            extra_ok = found_extra == set(truth["extra_in_2b"])
            print(f"\nChecked against answer key: {total - len(wrong)}/{total} statuses correct, "
                  f"extra-in-2B list {'correct' if extra_ok else 'WRONG'}")
        for inv_no, exp, got in wrong:
            print(f"  {inv_no}: expected {exp}, got {got}")


if __name__ == "__main__":
    main()
