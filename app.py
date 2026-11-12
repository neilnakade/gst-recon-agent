"""Simple web page for the GST reconciliation agent.

Run from the repo root:
    streamlit run app.py
"""
import hmac
import json
import os
import tempfile
import time
import uuid
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


# Optional pilot password: when APP_PASSWORD is set, the public sample stays open to everyone,
# but uploading files and the AI features unlock only after the password is typed in the sidebar.
APP_PASSWORD = get_secret("APP_PASSWORD")


def ai_allowed() -> bool:
    return not APP_PASSWORD or st.session_state.get("unlocked", False)


MAX_INVOICES = int(get_secret("MAX_INVOICES") or 30)  # protects the API quota in a shared pilot

NO_KEY_MESSAGE = ("No Gemini API key is available. Paste a key in the sidebar, or run the app on your own "
                  "computer with a key in a .env file. (The public demo has no key.)")

LABEL_TO_FILL = {report.STATUS_LABEL[k]: v for k, v in report.STATUS_FILL.items()}

# ---------------- Sidebar ----------------
company = st.sidebar.text_input("Your company name", "ABC Traders")
mode = st.sidebar.radio("Data", ["Sample data", "Upload my own"])
my_gstin = st.sidebar.text_input("Your own GSTIN (optional)", "",
                                 help="Lets the safety net catch invoices where your details were read as the supplier's.")
st.sidebar.caption("Sample data = the fake invoices made by synthetic.py. No real client data.")
user_key = st.sidebar.text_input("Your Gemini API key (optional)", type="password",
                                 help="Used only for this session and never saved by the app.")
with st.sidebar.expander("Privacy: where your data goes"):
    st.markdown(
        "- Your GSTR-2B file is read by this app only.\n"
        "- Invoice PDFs (their text or page images) are sent to Google Gemini using the key above.\n"
        "- The app does not save your files.\n"
        "- On a hosted copy, files pass through the host's servers. For real client data, run the app on your own computer (see PRIVACY.md).\n"
        "- A free/unpaid Gemini key lets Google use and review what is sent. For real client data use a billing-enabled (paid) key.")
if APP_PASSWORD:
    if st.session_state.get("unlocked"):
        st.sidebar.success("Pilot features unlocked")
    else:
        typed = st.sidebar.text_input("Pilot password (unlocks upload and AI features)", type="password")
        if typed:
            if hmac.compare_digest(typed.encode(), str(APP_PASSWORD).encode()):
                st.session_state["unlocked"] = True
                st.rerun()
            else:
                st.sidebar.error("Wrong password.")


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
    if not ai_allowed():
        st.info("Uploading your own files is locked in the public demo. Enter the pilot password in the sidebar.")
        st.stop()
    pdfs = st.file_uploader("Purchase invoices (PDF)", type="pdf", accept_multiple_files=True)
    portal_file = st.file_uploader("GSTR-2B data (JSON, Excel .xlsx or CSV)", type=["json", "xlsx", "csv"])
    st.caption("Extraction waits a few seconds per invoice to respect free-tier limits.")

    if st.button("Extract invoices", disabled=not pdfs):
        from google import genai
        key = user_key or get_secret("GEMINI_API_KEY")
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
        portal, info = gstr2b.parse_bytes(portal_file.getvalue(), portal_file.name)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
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
