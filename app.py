import concurrent.futures
import json
import os
import tempfile
import threading
import time
import uuid

import dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from werkzeug.middleware.proxy_fix import ProxyFix

import httplib2
from google_auth_httplib2 import AuthorizedHttp

dotenv.load_dotenv()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24).hex())
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

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

_tasks: dict[str, dict] = {}
_TASK_DIR = os.path.join(tempfile.gettempdir(), "gmail-cleaner-tasks")

def _task_save(task_id, data):
    _tasks[task_id] = data
    os.makedirs(_TASK_DIR, exist_ok=True)
    path = os.path.join(_TASK_DIR, task_id)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        app.logger.error("task_save(%s) failed: %s", task_id, e)

def _task_load(task_id):
    data = _tasks.get(task_id)
    if data is not None:
        return data
    path = os.path.join(_TASK_DIR, task_id)
    try:
        with open(path) as f:
            data = json.load(f)
            _tasks[task_id] = data
            return data
    except FileNotFoundError:
        return None
    except Exception as e:
        app.logger.error("task_load(%s) failed: %s", task_id, e)
        return None


def _redirect_uri():
    uri = OAUTH_REDIRECT_URI
    if uri:
        return uri
    return request.url_root.rstrip("/") + "/oauth2callback"


def _google_flow():
    uri = _redirect_uri()
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
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    return build("gmail", "v1", http=http)


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


@app.context_processor
def _inject_auth():
    return {"authenticated": _get_service() is not None}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/overview")
def overview():
    service = _get_service()
    if not service:
        return redirect(url_for("index"))
    return render_template("overview.html")


@app.route("/login")
def login():
    flow = _google_flow()
    flow.autogenerate_code_verifier = False
    auth_url, _ = flow.authorization_url(prompt="select_account")
    return redirect(auth_url)


@app.route("/testsess")
def testsess():
    count = session.get("count", 0) + 1
    session["count"] = count
    session["test_data"] = "x" * 2000
    return jsonify({"count": count, "sid": request.cookies.get("session", "none")})

@app.route("/debug")
def debug():
    creds = _get_credentials()
    return jsonify({
        "has_creds": creds is not None,
        "creds_valid": creds.valid if creds else None,
        "session_keys": list(session.keys()),
        "session_type": app.config.get("SESSION_TYPE"),
    })

@app.route("/oauth2callback")
def oauth2callback():
    error = request.args.get("error")
    if error:
        return render_template("index.html", error=f"Google denied access: {error}")

    scheme = request.headers.get("X-Forwarded-Proto", "https")
    auth_response = f"{scheme}://{request.host}{request.full_path}"
    if auth_response.endswith("?"):
        auth_response = auth_response[:-1]

    flow = _google_flow()
    try:
        flow.fetch_token(authorization_response=auth_response)
        creds = flow.credentials
        _save_credentials(creds)
        app.logger.info("OAUTH_OK: valid=%s expiry=%s", creds.valid, creds.expiry)
    except Exception as e:
        app.logger.error("OAUTH_ERR: %s", str(e))
        return render_template("index.html", authenticated=False, error=f"Login failed: {e}")

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
        return render_template("index.html", error="Keyword is required.")

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
        return render_template("index.html", error=str(e))


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
    _task_save(task_id, {"status": "running", "total": len(message_ids), "deleted": 0})

    def _run_delete(tid, ids):
        try:
            deleted = 0
            for i in range(0, len(ids), BATCH_SIZE):
                batch = ids[i : i + BATCH_SIZE]
                service.users().messages().batchDelete(userId="me", body={"ids": batch}).execute()
                deleted += len(batch)
                _task_save(tid, {"status": "running", "total": len(ids), "deleted": deleted})
                time.sleep(0.25)
            _task_save(tid, {"status": "done", "total": len(ids), "deleted": deleted})
        except Exception as e:
            prev = _task_load(tid) or {}
            _task_save(tid, {"status": "error", "total": len(message_ids), "deleted": prev.get("deleted", 0), "error": str(e)})

    thread = threading.Thread(target=_run_delete, args=(task_id, message_ids), daemon=True)
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/delete-category", methods=["POST"])
def delete_category():
    service = _get_service()
    if not service:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json()
    category = data.get("category", "")
    query = CATEGORY_MAP.get(category, "")
    if category not in CATEGORY_MAP:
        return jsonify({"error": f"Unknown category: {category}"}), 400

    task_id = str(uuid.uuid4())
    _task_save(task_id, {"status": "collecting", "collected": 0, "deleted": 0, "total": 0})

    def _run(tid):
        try:
            all_ids = []
            page_token = None
            while True:
                resp = service.users().messages().list(
                    userId="me", q=query, maxResults=PAGE_SIZE, pageToken=page_token
                ).execute()
                for m in resp.get("messages", []):
                    all_ids.append(m["id"])
                page_token = resp.get("nextPageToken")
                _task_save(tid, {"status": "collecting", "collected": len(all_ids), "deleted": 0, "total": 0})
                if not page_token:
                    break

            total = len(all_ids)
            _task_save(tid, {"status": "deleting", "collected": total, "deleted": 0, "total": total})
            deleted = 0
            for i in range(0, total, BATCH_SIZE):
                batch = all_ids[i:i + BATCH_SIZE]
                service.users().messages().batchDelete(userId="me", body={"ids": batch}).execute()
                deleted += len(batch)
                _task_save(tid, {"status": "deleting", "collected": total, "deleted": deleted, "total": total})
                time.sleep(0.25)

            _task_save(tid, {"status": "done", "collected": total, "deleted": deleted, "total": total})
        except Exception as e:
            prev = _task_load(tid) or {}
            _task_save(tid, {"status": "error", "collected": prev.get("collected", 0), "deleted": prev.get("deleted", 0), "total": prev.get("total", 0), "error": str(e)})

    thread = threading.Thread(target=_run, args=(task_id,), daemon=True)
    thread.start()
    return jsonify({"task_id": task_id})


@app.route("/progress/<task_id>")
def progress(task_id):
    task = _task_load(task_id)
    if not task:
        return jsonify({"status": "not_found"}), 404
    return jsonify(task)


_tl = threading.local()

def _get_service_tl(creds_json=""):
    if not hasattr(_tl, "svc") or _tl.svc is None:
        if not creds_json:
            svc = _get_service()
        else:
            creds = Credentials.from_authorized_user_info(json.loads(creds_json), SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(GoogleRequest())
            http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
            svc = build("gmail", "v1", http=http)
        _tl.svc = svc
    return _tl.svc

def _fetch_from(msg_id, creds_json):
    svc = _get_service_tl(creds_json)
    if not svc:
        return None
    try:
        msg = svc.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From"]
        ).execute()
        for h in msg.get("payload", {}).get("headers", []):
            if h["name"] == "From":
                val = h["value"]
                if "<" in val:
                    val = val.split("<")[1].rstrip(">")
                return val.lower()
    except Exception:
        pass
    return None


@app.route("/api/category-stats", methods=["POST"])
def category_stats():
    service = _get_service()
    if not service:
        return jsonify({"error": "Not authenticated"}), 401
    creds_json = session.get("gmail_creds", "")

    task_id = str(uuid.uuid4())
    stats = {
        "status": "scanning", "categories": {},
        "current": "Starting scan...",
        "overall_total": 0
    }
    _task_save(task_id, stats)

    def _run(tid):
        try:
            categories = ["primary", "promotions", "updates", "social", "forums"]
            results = {}
            overall = 0

            for cat in categories:
                query = CATEGORY_MAP.get(cat, "")
                _task_save(tid, {
                    "status": "scanning", "categories": results,
                    "current": f"Scanning {cat}... (listing messages)",
                    "overall_total": overall,
                })

                cat_ids = []
                page_token = None
                while True:
                    resp = service.users().messages().list(
                        userId="me", q=query, maxResults=PAGE_SIZE, pageToken=page_token
                    ).execute()
                    for m in resp.get("messages", []):
                        cat_ids.append(m["id"])
                    page_token = resp.get("nextPageToken")
                    if not page_token:
                        break

                total_est = len(cat_ids)
                _task_save(tid, {
                    "status": "scanning", "categories": results,
                    "current": f"{cat}: {total_est} messages, extracting senders...",
                    "overall_total": overall + total_est,
                })

                senders_set = set()
                ex = concurrent.futures.ThreadPoolExecutor(max_workers=8)
                try:
                    for i in range(0, len(cat_ids), 200):
                        chunk = cat_ids[i:i+200]
                        futures = [ex.submit(_fetch_from, mid, creds_json) for mid in chunk]
                        done, _ = concurrent.futures.wait(futures, timeout=30)
                        for f in done:
                            try:
                                s = f.result()
                                if s:
                                    senders_set.add(s)
                            except Exception:
                                pass
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)

                _task_save(tid, {
                    "status": "scanning", "categories": results,
                    "current": f"{cat}: {len(senders_set)} unique senders, counting...",
                    "overall_total": overall + total_est,
                })

                sender_counts = {}
                for idx, sender in enumerate(sorted(senders_set)):
                    try:
                        sq = f"from:{sender} {query}".strip()
                        sr = service.users().messages().list(
                            userId="me", q=sq, maxResults=1
                        ).execute()
                        c = sr.get("resultSizeEstimate", 0)
                        if c > 0:
                            sender_counts[sender] = c
                    except Exception:
                        pass
                    if (idx + 1) % 20 == 0:
                        _task_save(tid, {
                            "status": "scanning", "categories": results,
                            "current": f"{cat}: counted {idx+1}/{len(senders_set)} senders...",
                            "overall_total": overall + total_est,
                        })

                sorted_senders = dict(sorted(sender_counts.items(), key=lambda x: -x[1]))
                results[cat] = {
                    "total": total_est,
                    "senders": sorted_senders,
                }
                overall += total_est
                _task_save(tid, {
                    "status": "scanning", "categories": results,
                    "current": f"{cat} done ({total_est} emails, {len(sorted_senders)} senders)",
                    "overall_total": overall,
                })

            _task_save(tid, {
                "status": "done", "categories": results,
                "current": "", "overall_total": overall,
            })
        except Exception as e:
            _task_save(tid, {
                "status": "error",
                "categories": locals().get("results", {}),
                "error": str(e), "current": "", "overall_total": 0,
            })

    thread = threading.Thread(target=_run, args=(task_id,), daemon=True)
    thread.start()
    return jsonify({"task_id": task_id})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
