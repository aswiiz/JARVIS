import base64
import hmac
import logging
import os
import re
import time
import uuid
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jarvis.app")

from db import make_store  # noqa: E402
from jarvis_core import Jarvis, MissingConfig  # noqa: E402
from keepalive import start_keepalive  # noqa: E402

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024
CORS(app, origins=os.getenv("CORS_ORIGINS", "*"))

store = make_store()
jarvis = Jarvis()
STARTED = time.time()

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_FILES = 4

try:
    store.cleanup(int(os.getenv("FILE_TTL_DAYS", "7")))
except Exception as exc:  # cleanup is best-effort
    log.warning("cleanup skipped: %s", exc)


def require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = os.getenv("APP_API_TOKEN", "").strip()
        if not expected:
            if os.getenv("ALLOW_NO_AUTH", "").lower() in ("1", "true", "yes"):
                return fn(*args, **kwargs)
            return jsonify(error="Server is missing APP_API_TOKEN"), 503
        given = request.headers.get("X-API-Token", "")
        auth = request.headers.get("Authorization", "")
        if not given and auth.lower().startswith("bearer "):
            given = auth[7:]
        if not hmac.compare_digest(given.encode(), expected.encode()):
            return jsonify(error="Invalid or missing token"), 401
        return fn(*args, **kwargs)

    return wrapper


@app.get("/")
def index():
    return jsonify(name="JARVIS backend", status="ok", docs="POST /chat, GET /health, GET /history/<session_id>")


@app.get("/health")
def health():
    return jsonify(
        status="ok",
        storage=store.kind,
        uptime_seconds=int(time.time() - STARTED),
        **jarvis.status(),
    )


def _read_files():
    files = []
    if (request.content_type or "").startswith("multipart/"):
        message = (request.form.get("message") or "").strip()
        session_id = request.form.get("session_id") or ""
        for f in request.files.getlist("files")[:MAX_FILES]:
            files.append({"name": f.filename or "file", "mime": f.mimetype or "application/octet-stream", "data": f.read()})
    else:
        body = request.get_json(silent=True) or {}
        message = (body.get("message") or "").strip()
        session_id = body.get("session_id") or ""
        for f in (body.get("files") or [])[:MAX_FILES]:
            try:
                raw = str(f.get("data", ""))
                data = base64.b64decode(raw.split(",")[-1], validate=False)
            except Exception:
                raise ValueError("A file has invalid base64 data")
            files.append({"name": f.get("name") or "file", "mime": f.get("mime") or "application/octet-stream", "data": data})
    return message, session_id, files


@app.post("/chat")
@require_token
def chat():
    try:
        message, session_id, files = _read_files()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    if not message and not files:
        return jsonify(error="Send a 'message' or at least one file"), 400
    if not message:
        message = "Please analyze the attached file(s)."
    if not SESSION_RE.match(session_id):
        session_id = uuid.uuid4().hex

    try:
        history = store.history(session_id, 10)
        base_url = request.url_root.rstrip("/")
        result = jarvis.run(message, history, files, store, base_url)
        store.add_message(session_id, "user", message, {"files": [f["name"] for f in files]})
        store.add_message(session_id, "assistant", result["response"], {"tasks": result["tasks"]})
    except MissingConfig as exc:
        return jsonify(error=str(exc)), 503
    except Exception as exc:
        log.exception("chat failed")
        return jsonify(error=f"Request failed: {str(exc)[:200]}"), 500

    return jsonify(session_id=session_id, **result)


@app.get("/history/<session_id>")
@require_token
def history(session_id):
    if not SESSION_RE.match(session_id):
        return jsonify(error="Invalid session id"), 400
    return jsonify(session_id=session_id, messages=store.history(session_id, 100))


@app.get("/files/<token>")
def files(token):
    # Unguessable random token acts as the capability, so <img>/<audio> tags can load it.
    found = store.get_file(token)
    if not found:
        return jsonify(error="Not found"), 404
    data, mime = found
    return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=3600"})


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f"Upload too large (limit {os.getenv('MAX_UPLOAD_MB', '10')} MB)"), 413


# Start the continuous keep-alive loop (run gunicorn with --workers 1).
start_keepalive()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
