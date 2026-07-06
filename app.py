import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import dotenv
import flask
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from werkzeug.middleware.proxy_fix import ProxyFix

dotenv.load_dotenv()

# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24).hex())

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI")

SCOPES = ["https://mail.google.com/"]
BATCH_SIZE = 1000
PAGE_SIZE = 500

CATEGORY_MAP = {
    "all": "",
    "promotions": "category:promotions",
    "updates": "category:updates",
    "social": "category:social",
    "forums": "category:forums",
    "primary": "category:primary",
}

# In-memory task progress store
_tasks: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _redirect_uri():
    uri = OAUTH_REDIRECT_URI
    if uri:
        return uri
    return request.url_root.rstrip("/") + "/oauth2callback"

def _google_flow(redirect_uri=None):
    uri = redirect_uri or _redirect_uri()
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [uri],
            }
        },
        scopes=SCOPES,
    )
    flow.redirect_uri = uri
    return flow


def _get_credentials():
    creds_json = session.get("gmail_creds")
    if not creds_json:
        return None
    try:
        return Credentials.from_authorized_user_info(json.loads(creds_json), SCOPES)
    except (ValueError, KeyError):
        session.pop("gmail_creds", None)
        return None


def _save_credentials(creds):
    session["gmail_creds"] = creds.to_json()


def _get_service():
    creds = _get_credentials()
    if not creds:
        return None
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        _save_credentials(creds)
    if not creds.valid:
        return None
    return build("gmail", "v1", credentials=creds)


def _build_query(keyword, category, before, after):
    parts = [keyword]
    cat_filter = CATEGORY_MAP.get(category)
    if cat_filter:
        parts.append(cat_filter)
    if before:
        parts.append(f"before:{before}")
    if after:
        parts.append(f"after:{after}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    creds = _get_credentials()
    service = _get_service() if creds else None
    return render_template("index.html", authenticated=service is not None)


@app.route("/login")
def login():
    flow = _google_flow()
    flow.autogenerate_code_verifier = False
    auth_url, _ = flow.authorization_url(prompt="select_account")
    return redirect(auth_url)


@app.route("/oauth2callback")
def oauth2callback():
    flow = _google_flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    _save_credentials(creds)
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.pop("gmail_creds", None)
    return redirect(url_for("index"))


@app.route("/search", methods=["POST"])
def search():
    service = _get_service()
    if not service:
        return redirect(url_for("index"))

    keyword = request.form.get("keyword", "").strip()
    if not keyword:
        return render_template("index.html", authenticated=True, error="Keyword is required.")

    category = request.form.get("category", "all")
    before = request.form.get("before", "")
    after = request.form.get("after", "")
    query = _build_query(keyword, category, before, after)

    try:
        page_token = None
        all_ids = []
        while True:
            resp = service.users().messages().list(
                userId="me", q=query, maxResults=PAGE_SIZE, pageToken=page_token
            ).execute()
            messages = resp.get("messages", [])
            for msg in messages:
                all_ids.append(msg["id"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return render_template(
            "results.html",
            total=len(all_ids),
            message_ids=json.dumps(all_ids),
            keyword=keyword,
            category=category,
            before=before,
            after=after,
        )
    except Exception as e:
        return render_template("index.html", authenticated=True, error=str(e))


@app.route("/delete", methods=["POST"])
def delete():
    service = _get_service()
    if not service:
        return redirect(url_for("index"))

    data = request.get_json()
    message_ids = data.get("message_ids", [])
    if not message_ids:
        return jsonify({"error": "No messages to delete"}), 400

    task_id = str(uuid.uuid4())
    _tasks[task_id] = {"status": "running", "total": len(message_ids), "deleted": 0}

    def _run_delete(tid, ids):
        try:
            deleted = 0
            for i in range(0, len(ids), BATCH_SIZE):
                batch = ids[i : i + BATCH_SIZE]
                service.users().messages().batchDelete(userId="me", body={"ids": batch}).execute()
                deleted += len(batch)
                _tasks[tid] = {"status": "running", "total": len(ids), "deleted": deleted}
                time.sleep(0.25)
            _tasks[tid] = {"status": "done", "total": len(ids), "deleted": deleted}
        except Exception as e:
            _tasks[tid] = {"status": "error", "total": len(message_ids), "deleted": _tasks[tid]["deleted"], "error": str(e)}

    thread = threading.Thread(target=_run_delete, args=(task_id, message_ids), daemon=True)
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/progress/<task_id>")
def progress(task_id):
    task = _tasks.get(task_id)
    if not task:
        return jsonify({"status": "not_found"}), 404
    return jsonify(task)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
