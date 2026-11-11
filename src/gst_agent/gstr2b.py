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

Excel (.xlsx) and CSV files are read too (the portal also offers an Excel download, and many firms
use one). Columns are found by their headings ("GSTIN of supplier", "Invoice number", "Taxable value",
"Integrated/Central/State tax", ...), so slightly different layouts still work. The portal's Excel layout
has been checked only against descriptions, not a real file. Old .xls files must be re-saved as .xlsx.

Make realistic test files from the synthetic portal data:
    python -m src.gst_agent.gstr2b --make-sample           (official JSON structure)
    python -m src.gst_agent.gstr2b --make-sample-excel     (Excel, portal-like layout)
"""
import argparse
import csv
import io
import json
import re
from datetime import date, datetime
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


GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
DATE_FORMATS = ["%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d.%m.%Y", "%d/%m/%y"]


def _num(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("\u20b9", "").replace("Rs.", "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def _date_iso(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return text


def _classify(header: str) -> str | None:
    """Which of our fields does this column heading mean? None if it is not one we need."""
    h = re.sub(r"\s+", " ", header.lower().replace("\u20b9", "").replace("(", " ").replace(")", " ")).strip()
    if not h:
        return None
    if "itc" in h and ("availab" in h) and "tax" not in h:
        return "itc_available"
    if any(w in h for w in ("itc", "availed", "eligib", "irn", "filing", "period", "reason", "source", "recipient", "e-commerce")):
        return None
    if "gstin" in h:
        return "supplier_gstin"
    if "trade" in h or "legal name" in h or h in ("supplier name", "name"):
        return "supplier_name"
    if "invoice" in h and ("number" in h or "no." in h or re.search(r"\bno\b", h)):
        return "invoice_no"
    if "document number" in h or "document no" in h:
        return "invoice_no"
    if ("invoice" in h and "date" in h) or h == "date":
        return "invoice_date"
    if "taxable" in h:
        return "taxable_value"
    if "integrated" in h or "igst" in h:
        return "igst"
    if "central" in h or "cgst" in h:
        return "cgst"
    if ("state" in h and "tax" in h) or "sgst" in h or "utgst" in h:
        return "sgst"
    return None


def _parse_table(rows: list[list]) -> list[dict] | None:
    """One sheet or CSV as a list of rows -> flat invoice records, or None if this is not an invoice table.
    Several rows with the same GSTIN and invoice number (one per tax rate) are added together."""
    first = next((i for i, r in enumerate(rows) for c in r
                  if isinstance(c, str) and GSTIN_RE.match(c.strip().upper())), None)
    if first is None:
        return None
    width = max(len(r) for r in rows)
    header_rows = rows[max(0, first - 3):first]  # headings can be spread over up to three rows
    columns: dict[str, int] = {}
    for col in range(width):
        text = " ".join(str(r[col]) for r in header_rows if col < len(r) and r[col] not in (None, ""))
        field = _classify(text)
        if field and field not in columns:
            columns[field] = col
    needed = ["supplier_gstin", "invoice_no", "invoice_date", "taxable_value"]
    if any(f not in columns for f in needed):
        return None

    merged: dict[tuple, dict] = {}
    for r in rows[first:]:
        def cell(field):
            c = columns.get(field)
            return r[c] if c is not None and c < len(r) else None
        gstin = str(cell("supplier_gstin") or "").strip().upper()
        inum = str(cell("invoice_no") or "").strip()
        if not GSTIN_RE.match(gstin) or not inum:
            continue
        rec = merged.setdefault((gstin, inum), {
            "supplier_gstin": gstin, "supplier_name": str(cell("supplier_name") or ""),
            "invoice_no": inum, "invoice_date": _date_iso(cell("invoice_date")),
            "taxable_value": 0.0, "cgst": 0.0, "sgst": 0.0, "igst": 0.0,
            "section": "table", "itc_available": str(cell("itc_available") or "")})
        for f in ("taxable_value", "cgst", "sgst", "igst"):
            rec[f] = round(rec[f] + _num(cell(f)), 2)
    return list(merged.values())


def _parse_spreadsheet(raw: bytes, filename: str) -> tuple[list[dict], dict]:
    name = filename.lower()
    sheets: dict[str, list[list]] = {}
    if name.endswith(".csv"):
        text = raw.decode("utf-8-sig", errors="replace")
        sheets["csv"] = [row for row in csv.reader(io.StringIO(text))]
    else:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
        for ws in wb.worksheets:
            sheets[ws.title] = [list(r) for r in ws.iter_rows(values_only=True)]

    def kind(title: str) -> str:
        t = title.lower()
        return "notes" if "cdn" in t else "invoices" if "b2b" in t else "other"

    records, used, skipped, buyer = [], [], {}, ""
    for title, rows in sheets.items():
        if kind(title) == "notes":
            parsed = _parse_table(rows) or []
            n = sum(1 for r in rows for c in r if isinstance(c, str) and GSTIN_RE.match(c.strip().upper()))
            if n:
                skipped[title] = n
        elif kind(title) == "invoices":
            parsed = _parse_table(rows)
            if parsed:
                records += parsed; used.append(title)
    if not records:  # no sheet is called B2B: take any sheet (or the CSV) that looks like an invoice table
        for title, rows in sheets.items():
            if kind(title) == "notes":
                continue
            parsed = _parse_table(rows)
            if parsed:
                records += parsed; used.append(title)
    if not records:
        raise ValueError("Could not find an invoice table (GSTIN, invoice number, date, taxable value) in this "
                         f"file. Sheets found: {', '.join(sheets) or 'none'}.")
    for title, rows in sheets.items():  # your own GSTIN is usually on the first sheet next to a "GSTIN" label
        if title in used or buyer:
            continue
        for r in rows[:15]:
            labels = [str(c).lower() for c in r if isinstance(c, str)]
            if any("gstin" in l for l in labels):
                buyer = next((c.strip().upper() for c in r if isinstance(c, str)
                              and GSTIN_RE.match(c.strip().upper())), "")
                if buyer:
                    break
    return records, {"buyer_gstin": buyer, "period": "", "invoices": len(records),
                     "skipped": skipped, "source": "spreadsheet", "sheets": used}


def parse_bytes(raw: bytes, filename: str) -> tuple[list[dict], dict]:
    """Read a GSTR-2B (or purchase-style) file from raw bytes: JSON, Excel (.xlsx) or CSV."""
    name = filename.lower()
    if name.endswith(".xls"):
        raise ValueError("Old .xls files are not supported. Open it in Excel and save it as .xlsx, then try again.")
    if name.endswith((".xlsx", ".xlsm", ".csv")):
        return _parse_spreadsheet(raw, filename)
    return parse_any(json.loads(raw.decode("utf-8-sig")))


def load_portal(path: Path) -> tuple[list[dict], dict]:
    path = Path(path)
    return parse_bytes(path.read_bytes(), path.name)


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


def make_excel_sample(rows: list[dict], names: dict, buyer_gstin: str, out_path: Path) -> None:
    """A portal-like Excel file: a summary sheet, a B2B sheet with two heading rows, one row per tax rate
    (every third invoice is split over two rates' worth of rows), and a credit-note sheet."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Read me"
    ws.append(["Goods and Services Tax - GSTR-2B"])
    ws.append(["GSTIN", buyer_gstin])
    ws.append(["Return period", "092026"])

    b2b = wb.create_sheet("B2B")
    b2b.append(["Taxable Inward supplies received from registered persons"])
    b2b.append([])
    b2b.append(["GSTIN of supplier", "Trade/Legal name", "Invoice Details", None, None, None, None, None, None,
                "Tax Amount", None, None, None, "GSTR-1/IFF/GSTR-5 Period", "ITC Availability"])
    b2b.append([None, None, "Invoice number", "Invoice type", "Invoice Date", "Invoice Value(\u20b9)",
                "Place of supply", "Rate(%)", "Taxable Value (\u20b9)", "Integrated Tax(\u20b9)", "Central Tax(\u20b9)",
                "State/UT Tax(\u20b9)", "Cess(\u20b9)", None, None])
    for n, r in enumerate(rows):
        d = date.fromisoformat(r["invoice_date"])
        shares = [1.0] if n % 3 else [0.6, 0.4]
        used = {k: 0.0 for k in ("taxable_value", "cgst", "sgst", "igst")}
        for k, share in enumerate(shares):
            last = k == len(shares) - 1
            part = {}
            for key in used:
                part[key] = round(r[key] - used[key], 2) if last else round(r[key] * share, 2)
                used[key] = round(used[key] + part[key], 2)
            tax = part["cgst"] + part["sgst"] + part["igst"]
            when = d if n % 2 else d.strftime("%d-%m-%Y")  # some real dates, some text, like real exports
            b2b.append([r["supplier_gstin"], names.get(r["supplier_gstin"], ""), r["invoice_no"], "Regular", when,
                        round(r["taxable_value"] + r["cgst"] + r["sgst"] + r["igst"], 2), buyer_gstin[:2],
                        _nearest_rate(part["taxable_value"], tax), part["taxable_value"], part["igst"],
                        part["cgst"], part["sgst"], 0, "092026", "Yes"])
    cdn = wb.create_sheet("B2B-CDNR")
    cdn.append(["GSTIN of supplier", "Trade/Legal name", "Note number", "Note Date", "Taxable Value (\u20b9)"])
    for g in list({r["supplier_gstin"] for r in rows})[:2]:
        cdn.append([g, names.get(g, ""), "CN/1", "15-09-2026", 1000])
    wb.save(out_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--make-sample", action="store_true",
                    help="write data/synthetic/gstr2b_official.json from the synthetic portal rows")
    ap.add_argument("--flat", default="data/synthetic/gstr2b.json")
    ap.add_argument("--truth", default="data/synthetic/ground_truth.json")
    ap.add_argument("--out", default="data/synthetic/gstr2b_official.json")
    ap.add_argument("--buyer-gstin", default="27AAAAA0000A1Z5")
    ap.add_argument("--make-sample-excel", action="store_true",
                    help="write data/synthetic/gstr2b_official.xlsx (portal-like Excel layout)")
    ap.add_argument("--file", help="a GSTR-2B file (JSON, .xlsx or .csv) to inspect")
    args = ap.parse_args()

    if args.make_sample:
        rows = json.loads(Path(args.flat).read_text())
        names = {i["supplier_gstin"]: i["supplier_name"]
                 for i in json.loads(Path(args.truth).read_text())["invoices"]}
        Path(args.out).write_text(json.dumps(make_official(rows, names, args.buyer_gstin), indent=2))
        print(f"Wrote {args.out} ({len(rows)} invoices in the official structure)")
    elif args.make_sample_excel:
        rows = json.loads(Path(args.flat).read_text())
        names = {i["supplier_gstin"]: i["supplier_name"]
                 for i in json.loads(Path(args.truth).read_text())["invoices"]}
        out = Path(args.out).with_suffix(".xlsx")
        make_excel_sample(rows, names, args.buyer_gstin, out)
        print(f"Wrote {out} (Excel, portal-like layout)")
    elif args.file:
        records, info = load_portal(Path(args.file))
        print(f"Read {len(records)} invoices. Period {info.get('period') or '?'}, "
              f"your GSTIN {info.get('buyer_gstin') or '?'}. Skipped sections: {info.get('skipped') or 'none'}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
