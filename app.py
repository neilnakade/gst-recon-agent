"""Simple web page for the GST reconciliation agent.

Run from the repo root:
    streamlit run app.py
"""
import hmac
import json
import os
import time
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.gst_agent import emailer, extractor, gstr2b, matcher, report, validate

load_dotenv()
st.set_page_config(page_title="GST Reconciliation Agent", layout="wide")
st.title("GST Reconciliation Agent")
st.caption("Match purchase invoices against GSTR-2B, find the differences, draft supplier follow-ups.")

def get_secret(name: str):
    """Look in environment variables / .env first, then in Streamlit's hosted secrets."""
    value = os.getenv(name)
    if value:
        return value
    try:
        return st.secrets.get(name)
    except Exception:
        return None


# Optional password gate for a private pilot copy: only active when APP_PASSWORD is set.
APP_PASSWORD = get_secret("APP_PASSWORD")
if APP_PASSWORD and not st.session_state.get("unlocked"):
    typed = st.text_input("Password", type="password")
    if typed:
        if hmac.compare_digest(typed.encode(), str(APP_PASSWORD).encode()):
            st.session_state["unlocked"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()

MAX_INVOICES = int(get_secret("MAX_INVOICES") or 30)  # protects the API quota in a shared pilot

NO_KEY_MESSAGE = ("This feature is switched off in the public demo, which has no AI key. "
                  "Run the app on your own computer with a free Gemini key in a .env file to use it.")

LABEL_TO_FILL = {report.STATUS_LABEL[k]: v for k, v in report.STATUS_FILL.items()}

# ---------------- Sidebar ----------------
company = st.sidebar.text_input("Your company name", "ABC Traders")
mode = st.sidebar.radio("Data", ["Sample data", "Upload my own"])
my_gstin = st.sidebar.text_input("Your own GSTIN (optional)", "",
                                 help="Lets the safety net catch invoices where your details were read as the supplier's.")
st.sidebar.caption("Sample data = the fake invoices made by synthetic.py. No real client data.")


# ---------------- Get books + portal ----------------
def get_sample() -> tuple[list[dict], list[dict]]:
    # sample_data/ is committed to the repo (so a hosted demo works);
    # data/ is the local, generated copy.
    candidates = [(Path("sample_data/extracted"), Path("sample_data/gstr2b.json")),
                  (Path("data/extracted"), Path("data/synthetic/gstr2b.json"))]
    for extracted, portal_path in candidates:
        if extracted.exists() and portal_path.exists():
            return matcher.load_books(extracted), json.loads(portal_path.read_text())
    st.error("Sample data not found. Run synthetic.py and the extractor first.")
    st.stop()


def get_uploaded() -> tuple[list[dict], list[dict]]:
    pdfs = st.file_uploader("Purchase invoices (PDF)", type="pdf", accept_multiple_files=True)
    portal_file = st.file_uploader("GSTR-2B data (JSON)", type="json")
    st.caption("Extraction waits a few seconds per invoice to respect free-tier limits.")

    if st.button("Extract invoices", disabled=not pdfs):
        from google import genai
        key = get_secret("GEMINI_API_KEY")
        if not key:
            st.info(NO_KEY_MESSAGE)
            st.stop()
        if len(pdfs) > MAX_INVOICES:
            st.warning(f"Only the first {MAX_INVOICES} invoices will be processed in this copy.")
            pdfs = pdfs[:MAX_INVOICES]
        client = genai.Client(api_key=key)
        books, failed, bar = [], [], st.progress(0.0)
        for i, f in enumerate(pdfs):
            try:
                text = extractor.read_pdf_text(f)
                if text:
                    fields = extractor.extract_invoice(client, text)
                else:  # scanned invoice: no text layer, so let Gemini read the page image
                    f.seek(0)
                    fields = extractor.extract_invoice_image(client, extractor.page_to_png(f))
                books.append(fields.model_dump())
            except Exception as e:
                failed.append(f"{f.name} ({type(e).__name__})")
            bar.progress((i + 1) / len(pdfs))
            time.sleep(8)
        st.session_state["uploaded_books"] = books
        if failed:
            st.warning("Could not read: " + ", ".join(failed))

    books = st.session_state.get("uploaded_books", [])
    if not books or portal_file is None:
        st.info("Upload invoice PDFs and a GSTR-2B JSON file, then click 'Extract invoices'.")
        st.stop()
    try:
        portal, info = gstr2b.parse_any(json.load(portal_file))
    except (ValueError, json.JSONDecodeError) as e:
        st.error(f"Could not read the GSTR-2B file: {e}")
        st.stop()
    if info:
        st.session_state["portal_info"] = info
    else:
        st.session_state.pop("portal_info", None)
    return books, portal


books, portal = get_sample() if mode == "Sample data" else get_uploaded()

# ---------------- Reconcile ----------------
portal_info = st.session_state.get("portal_info", {}) if mode == "Upload my own" else {}
if portal_info.get("skipped"):
    st.info("Official GSTR-2B read: " + f"{portal_info['invoices']} invoices matched against your books. "
            "Not matched (not purchase invoices): " + ", ".join(f"{k} ({v})" for k, v in portal_info["skipped"].items()))
clean_books, review = validate.split_books(books, my_gstin or portal_info.get("buyer_gstin") or None)  # doubtful invoices are set aside
outcome = matcher.reconcile(clean_books, portal)
outcome["needs_review"] = review
results, extras = outcome["results"], outcome["extra_in_2b"]
books_by_no = {b["invoice_no"]: b for b in clean_books}

counts = {k: 0 for k in report.STATUS_LABEL}
for r in results:
    counts[r["status"]] += 1
at_risk = sum(report.tax_of(books_by_no[r["invoice_no"]]) for r in results
              if r["status"] == "missing_in_2b")

cols = st.columns(7)
for col, (key, label) in zip(cols, report.STATUS_LABEL.items()):
    col.metric(label, counts[key])
cols[4].metric("Extra in 2B", len(extras))
cols[5].metric("Needs review", len(review))
cols[6].metric("Tax at risk (Rs.)", f"{at_risk:,.0f}")


def table(rows: list[dict]) -> None:
    df = pd.DataFrame([{
        "Invoice no.": r["invoice_no"], "Supplier": r["supplier_name"],
        "GSTIN": r["supplier_gstin"], "Status": report.STATUS_LABEL[r["status"]],
        "What is wrong": report.explain(r)} for r in rows])
    if df.empty:
        st.success("Nothing here.")
        return
    styled = df.style.apply(
        lambda row: [f"background-color: #{LABEL_TO_FILL[row['Status']]}; color: black"] * len(row),
        axis=1)
    st.dataframe(styled, width="stretch", hide_index=True)


tab_all, tab_fu, tab_extra, tab_review, tab_mail, tab_dl = st.tabs(
    ["All invoices", "Needs follow-up", "Extra in 2B", "Needs review", "Supplier emails", "Download"])

with tab_review:
    st.caption("The AI could not read these reliably, so they were NOT matched. "
               "Check the original invoice and correct the data.")
    if review:
        st.dataframe(pd.DataFrame([{"File": r["source"], "Supplier (as read)": r["supplier_name"],
                                    "Invoice no. (as read)": r["invoice_no"],
                                    "Why it was set aside": "; ".join(r["reasons"])} for r in review]),
                     width="stretch", hide_index=True)
    else:
        st.success("Nothing needs review.")

with tab_all:
    table(results)
with tab_fu:
    table([r for r in results if r["status"] != "matched"])
with tab_extra:
    if review and extras:
        st.caption("Some of these may belong to invoices that were set aside for review.")
    if extras:
        st.dataframe(pd.DataFrame(extras), width="stretch", hide_index=True)
    else:
        st.success("Nothing here.")

with tab_mail:
    facts = emailer.build_facts(results, books_by_no)
    sig = json.dumps(facts, sort_keys=True) + company
    drafts = {s: emailer.template_email(s, f, company) for s, f in facts.items()}
    sources = {s: "template" for s in facts}

    if st.button("Rewrite with AI (Gemini)", disabled=not facts):
        from google import genai
        key = get_secret("GEMINI_API_KEY")
        if not key:
            st.info(NO_KEY_MESSAGE)
        else:
            client, ai = genai.Client(api_key=key), {}
            with st.spinner("Writing emails..."):
                for s, f in facts.items():
                    try:
                        e = emailer.ai_email(client, s, f, company)
                        if emailer.passes_guardrail(e, f):
                            ai[s] = e
                    except Exception:
                        pass
                    time.sleep(8)
            st.session_state["ai_emails"] = {"sig": sig, "emails": ai}

    saved = st.session_state.get("ai_emails")
    if saved and saved["sig"] == sig:
        for s, e in saved["emails"].items():
            drafts[s], sources[s] = e, "AI, passed fact check"

    if not facts:
        st.success("No problem invoices, so no emails are needed.")
    st.caption("These are drafts. Review before sending. Nothing is sent automatically.")
    for s, e in drafts.items():
        with st.expander(f"{s} ({len(facts[s]['issues'])} invoice(s), {sources[s]})"):
            st.text("Subject: " + e["subject"] + "\n\n" + e["body"])

with tab_dl:
    out = Path("output/gst_reconciliation_report.xlsx")
    report.build_report(outcome, books_by_no, out)
    st.download_button("Download Excel report", out.read_bytes(), file_name=out.name,
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
