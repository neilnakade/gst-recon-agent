"""Read a GSTR-2B JSON file in the structure used by the GST portal / GSTN API.

The structure (as shown in the GSTN API documentation, e.g. Vayana's GSTR-2B page):
    {"data": {"gstin": "<your GSTIN>", "rtnprd": "092026", ...,
              "docdata": {"b2b":  [{"ctin": "<supplier GSTIN>", "trdnm": "<name>",
                                    "inv": [{"inum": "...", "dt": "dd-mm-yyyy", "val": ...,
                                             "items": [{"txval": ..., "igst": ..., "cgst": ..., "sgst": ...}]}]}],
                          "cdnr": [...credit/debit notes...], ...}}}

This reader is tolerant about the outer wrapper (it searches for "docdata"), but it has only been
tested against that documented structure and a sample generated from it, NOT against a real file
downloaded from the portal. A real file may differ in small ways.

What it reads:  b2b (invoices) and b2ba (amended invoices).
What it skips:  credit/debit notes (cdnr, cdnra), ISD, imports and e-commerce sections. They are
                counted and reported, not matched, because they are not purchase invoices.

Make a realistic test file from the synthetic portal data:
    python -m src.gst_agent.gstr2b --make-sample
"""
import argparse
import json
from datetime import date
from pathlib import Path

INVOICE_SECTIONS = ["b2b", "b2ba"]
SKIPPED_SECTIONS = ["cdnr", "cdnra", "isd", "isda", "impg", "impgsez", "ecom", "ecoma"]
STANDARD_RATES = [0, 0.25, 1.5, 3, 5, 12, 18, 28, 40]


def _find(node, key):
    """Depth-first search for the first dict that has `key`."""
    if isinstance(node, dict):
        if key in node:
            return node
        for v in node.values():
            found = _find(v, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find(v, key)
            if found is not None:
                return found
    return None


def _iso(dmy: str) -> str:
    """'24-11-2026' -> '2026-11-24'. Anything unexpected is returned unchanged."""
    try:
        d, m, y = str(dmy).split("-")
        return date(int(y), int(m), int(d)).isoformat()
    except (ValueError, TypeError):
        return str(dmy)


def parse_gstr2b(data) -> tuple[list[dict], dict]:
    """Official structure -> (flat invoice records, info). Records use the same keys the matcher
    already uses: supplier_gstin, invoice_no, invoice_date, taxable_value, cgst, sgst, igst."""
    holder = _find(data, "docdata")
    if holder is None:
        raise ValueError("This does not look like a GSTR-2B file: no 'docdata' section found.")
    docdata = holder["docdata"]
    meta = _find(data, "rtnprd") or {}

    records = []
    for section in INVOICE_SECTIONS:
        for supplier in docdata.get(section) or []:
            for inv in supplier.get("inv") or []:
                items = inv.get("items") or []
                records.append({
                    "supplier_gstin": str(supplier.get("ctin", "")).strip().upper(),
                    "supplier_name": supplier.get("trdnm", ""),
                    "invoice_no": str(inv.get("inum", "")).strip(),
                    "invoice_date": _iso(inv.get("dt", "")),
                    "taxable_value": round(sum(float(i.get("txval", 0) or 0) for i in items), 2),
                    "cgst": round(sum(float(i.get("cgst", 0) or 0) for i in items), 2),
                    "sgst": round(sum(float(i.get("sgst", 0) or 0) for i in items), 2),
                    "igst": round(sum(float(i.get("igst", 0) or 0) for i in items), 2),
                    "section": section,
                    "itc_available": inv.get("itcavl", ""),
                })
    skipped = {}
    for section in SKIPPED_SECTIONS:
        n = 0
        for supplier in docdata.get(section) or []:
            n += len(supplier.get("nt") or supplier.get("inv") or supplier.get("doclist")
                     or supplier.get("boe") or [supplier])
        if n:
            skipped[section] = n
    info = {"buyer_gstin": str(meta.get("gstin", "")).strip().upper(),
            "period": meta.get("rtnprd", ""), "invoices": len(records), "skipped": skipped}
    return records, info


def parse_any(data) -> tuple[list[dict], dict]:
    """Accepts either our simple flat list (the synthetic data) or the official structure."""
    if isinstance(data, list):
        return data, {}
    return parse_gstr2b(data)


def load_portal(path: Path) -> tuple[list[dict], dict]:
    return parse_any(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------- sample generator (for testing)
def _nearest_rate(taxable: float, tax: float) -> float:
    pct = tax / taxable * 100 if taxable else 0
    return min(STANDARD_RATES, key=lambda r: abs(r - pct))


def make_official(rows: list[dict], names: dict, buyer_gstin: str, period: str = "092026") -> dict:
    """Wrap flat portal rows into the documented GSTR-2B structure. Every third invoice is split
    into two line items, and a couple of credit notes are added, to exercise the reader."""
    by_supplier: dict[str, list[dict]] = {}
    for n, r in enumerate(rows):
        d = date.fromisoformat(r["invoice_date"])
        parts = [(1.0,)] if n % 3 else [(0.6,), (0.4,)]
        items, used = [], {"txval": 0.0, "cgst": 0.0, "sgst": 0.0, "igst": 0.0}
        for k, (share,) in enumerate(parts, 1):
            last = k == len(parts)
            item = {"num": k}
            for key, src in (("txval", "taxable_value"), ("cgst", "cgst"), ("sgst", "sgst"), ("igst", "igst")):
                item[key] = round(r[src] - used[key], 2) if last else round(r[src] * share, 2)
                used[key] = round(used[key] + item[key], 2)
            item["rt"] = _nearest_rate(item["txval"], item["cgst"] + item["sgst"] + item["igst"])
            item["cess"] = 0
            items.append(item)
        tax = r["cgst"] + r["sgst"] + r["igst"]
        by_supplier.setdefault(r["supplier_gstin"], []).append({
            "inum": r["invoice_no"], "typ": "R", "dt": d.strftime("%d-%m-%Y"),
            "val": round(r["taxable_value"] + tax, 2), "pos": buyer_gstin[:2], "rev": "N",
            "itcavl": "Y", "rsn": "", "diffprcnt": 1, "items": items})
    b2b = [{"ctin": g, "trdnm": names.get(g, ""), "supfildt": "11-10-2026", "supprd": period, "inv": invs}
           for g, invs in by_supplier.items()]
    some = list(by_supplier)[:2]
    cdnr = [{"ctin": g, "trdnm": names.get(g, ""), "supfildt": "11-10-2026", "supprd": period,
             "nt": [{"ntnum": f"CN/{i + 1}", "typ": "C", "suptyp": "R", "dt": "15-09-2026", "val": 1180.0,
                     "pos": buyer_gstin[:2], "rev": "N", "itcavl": "Y", "rsn": "C", "diffprcnt": 1,
                     "items": [{"num": 1, "rt": 18, "txval": 1000, "igst": 0, "cgst": 90, "sgst": 90, "cess": 0}]}]}
            for i, g in enumerate(some)]
    return {"status": "1", "data": {"chksum": "SAMPLE", "data": {
        "gstin": buyer_gstin, "rtnprd": period, "version": "1.0", "gendt": "14-10-2026",
        "docdata": {"b2b": b2b, "cdnr": cdnr}}}, "error": None, "info": None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--make-sample", action="store_true",
                    help="write data/synthetic/gstr2b_official.json from the synthetic portal rows")
    ap.add_argument("--flat", default="data/synthetic/gstr2b.json")
    ap.add_argument("--truth", default="data/synthetic/ground_truth.json")
    ap.add_argument("--out", default="data/synthetic/gstr2b_official.json")
    ap.add_argument("--buyer-gstin", default="27AAAAA0000A1Z5")
    ap.add_argument("--file", help="a GSTR-2B JSON file to inspect")
    args = ap.parse_args()

    if args.make_sample:
        rows = json.loads(Path(args.flat).read_text())
        names = {i["supplier_gstin"]: i["supplier_name"]
                 for i in json.loads(Path(args.truth).read_text())["invoices"]}
        Path(args.out).write_text(json.dumps(make_official(rows, names, args.buyer_gstin), indent=2))
        print(f"Wrote {args.out} ({len(rows)} invoices in the official structure)")
    elif args.file:
        records, info = load_portal(Path(args.file))
        print(f"Read {len(records)} invoices. Period {info.get('period') or '?'}, "
              f"your GSTIN {info.get('buyer_gstin') or '?'}. Skipped sections: {info.get('skipped') or 'none'}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
