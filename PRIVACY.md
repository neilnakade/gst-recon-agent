# Privacy: where your data goes

Plain description of what this app does with data. This is not legal advice.

## What happens to your files
- **GSTR-2B file (JSON / Excel / CSV):** read by the app on the machine it runs on. It is not sent to any AI.
- **Purchase invoice PDFs:** the text (or page images for scans) is sent to Google's Gemini API to read the fields.
- **Matching, validation and the Excel report:** plain code, no AI, done on the machine running the app.
- The app does not save uploaded files. Reports are built in a temporary file and deleted.

## Where to run it
| Way of running | Who sees the data | Use for |
|---|---|---|
| On your own / the client's computer (`run_local.bat`) | Only that computer, plus Gemini for invoice reading | Real client data |
| Hosted demo (Streamlit Cloud) | Host's servers too | Fake/sample data only |

## Free vs paid Gemini key
- **Free (unpaid) key:** Google may use submitted content to improve its products and human reviewers may read it. Do not submit confidential data.
- **Paid (billing-enabled) key:** Google states prompts and responses are not used to improve its products and are processed under its data processing terms.
- Terms can change. Check Google's current Gemini API terms before relying on this.

## Good practice
1. Get the data owner's permission before processing their files.
2. For real data, the data owner creates their own paid key and uses it on their own computer.
3. Never share an API key in chat, screenshots or the repo (`.env` is git-ignored).
