"""TasPlan Review — Tasmanian planning assessment assistant (Flask app).

Analytical aid for qualified planners. NOT a statutory determination and NOT
legal advice. Every review is grounded ONLY in the ingested scheme clauses and
TASCAT decisions held in data/ — the engine never invents clause numbers,
standards, or holdings.

Single-file architecture inherited from the Will scaffold: routes + helpers +
the review engine all live here. Live ingestion lives in ingest/.
"""

import os
import re
import time
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, abort, Response, stream_with_context,
)
from werkzeug.security import check_password_hash

import config

TAS = ZoneInfo("Australia/Hobart")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod-2026")
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB

# The load-bearing caveat — surfaced in the UI and stamped on every review.
CAVEAT = "Analytical aid only; not a statutory determination or legal advice."

# ── Data helpers ────────────────────────────────────────────────────────────
# Uses Google Cloud Storage when GCS_BUCKET is set (production), otherwise the
# local data/ directory (development). Never open JSON files directly elsewhere.

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
GCS_BUCKET = os.environ.get("GCS_BUCKET")

_gcs_client = None
_gcs_bucket = None


def _get_bucket():
    global _gcs_client, _gcs_bucket
    if _gcs_bucket is None:
        from google.cloud import storage
        _gcs_client = storage.Client()
        _gcs_bucket = _gcs_client.bucket(GCS_BUCKET)
    return _gcs_bucket


def load_json(filename):
    if GCS_BUCKET:
        blob = _get_bucket().blob(filename)
        if not blob.exists():
            return {}
        return json.loads(blob.download_as_text())
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_json(filename, data):
    if GCS_BUCKET:
        blob = _get_bucket().blob(filename)
        blob.upload_from_string(json.dumps(data, indent=2), content_type="application/json")
        return
    path = os.path.join(DATA_DIR, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Binary blobs (image uploads) ──────────────────────────────────────────────
# Kept separate from load_json/save_json, which are for structured data only.
# Same GCS-vs-local switch, but bytes in/out and never JSON-encoded.

def save_bytes(filename, data, content_type):
    if GCS_BUCKET:
        blob = _get_bucket().blob(filename)
        blob.upload_from_string(data, content_type=content_type)
        return
    path = os.path.join(DATA_DIR, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def load_bytes(filename):
    if GCS_BUCKET:
        blob = _get_bucket().blob(filename)
        if not blob.exists():
            return None
        return blob.download_as_bytes()
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


# ── Image uploads (Ask Holly) ──────────────────────────────────────────────────
# Planners can attach site plans / drawings / map screenshots. Stored as blobs
# (not in any JSON), keyed by a random hex name so they can be served back and
# carried across the refine loop.
_UPLOAD_PREFIX = "uploads/"
_UPLOAD_MAX_BYTES = 8 * 1024 * 1024       # 8 MB per image
_UPLOAD_MAX_FILES = 4
_UPLOAD_MIMES = {"image/jpeg": "jpg", "image/png": "png",
                 "image/webp": "webp", "image/gif": "gif"}
_UPLOAD_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:jpg|png|webp|gif)$")


def _store_uploaded_images():
    """Read image files from the current request, validate, store each blob.
    Returns a list of {key, mime, name} refs. Best-effort — skips bad files."""
    import uuid
    refs = []
    for f in request.files.getlist("images"):
        if not f or not f.filename:
            continue
        mime = (f.mimetype or "").lower()
        ext = _UPLOAD_MIMES.get(mime)
        if not ext:
            continue
        data = f.read(_UPLOAD_MAX_BYTES + 1)
        if not data or len(data) > _UPLOAD_MAX_BYTES:
            continue
        key = f"{_UPLOAD_PREFIX}{uuid.uuid4().hex}.{ext}"
        try:
            save_bytes(key, data, mime)
            refs.append({"key": key, "mime": mime, "name": f.filename[:120]})
        except Exception as e:
            _log("ask.upload_error", error=str(e)[:200])
        if len(refs) >= _UPLOAD_MAX_FILES:
            break
    return refs


def _collect_images():
    """Merge image refs carried from a prior turn (hidden images_json) with any
    newly uploaded files. Returns a list of {key, mime, name}."""
    refs = []
    try:
        prior = json.loads(request.form.get("images_json") or "[]")
        if isinstance(prior, list):
            for p in prior:
                if isinstance(p, dict) and _UPLOAD_NAME_RE.match(
                        str(p.get("key", "")).split("/")[-1]):
                    refs.append({"key": str(p["key"]), "mime": str(p.get("mime", "")),
                                 "name": str(p.get("name", ""))})
    except (ValueError, TypeError):
        pass
    refs.extend(_store_uploaded_images())
    return refs[:_UPLOAD_MAX_FILES]


def _images_as_gemini_parts(image_refs):
    """Load stored images and return Gemini inlineData parts (base64-encoded)."""
    import base64
    parts = []
    for ref in image_refs or []:
        data = load_bytes(ref.get("key", ""))
        if not data:
            continue
        parts.append({"inlineData": {"mimeType": ref.get("mime") or "image/png",
                                     "data": base64.b64encode(data).decode()}})
    return parts


# ── Auth ────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        users = load_json("users.json")
        user = users.get(session["user"])
        if not user or user.get("role") != "admin":
            abort(403)
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user" in session:
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        users = load_json("users.json")
        user = users.get(username)
        if user and check_password_hash(user["password"], password):
            session["user"] = username
            session["name"] = user.get("name", username)
            session["role"] = user.get("role", "user")
            user["last_login"] = datetime.now(tz=TAS).isoformat()
            save_json("users.json", users)
            return redirect(request.form.get("next") or url_for("home"))
        error = "Invalid credentials. Please try again."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Context processor ─────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    try:
        app_config = load_json("app_config.json")
    except Exception:
        app_config = {}
    return {
        "now": datetime.now(tz=TAS),
        "app_config": app_config,
        "current_user": session.get("user"),
        "current_name": session.get("name"),
        "current_role": session.get("role"),
        "caveat": CAVEAT,
    }


@app.after_request
def _no_store_html(resp):
    """Never cache rendered HTML. Several pages carry their JS/CSS inline in the
    server-rendered HTML (e.g. the council's mdLite renderer); without this the
    browser serves a cached page and runs stale inline scripts after a deploy.
    Static assets keep their own caching."""
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


def _md_lite(text):
    """Render a safe subset of markdown for server-rendered answers (Ask Holly):
    escape HTML first, then add only our own tags — the same transforms as the
    council's client-side mdLite, so model output can't inject HTML."""
    from markupsafe import Markup, escape
    t = str(escape(text or ""))
    t = re.sub(r"(?m)^\s*([-*_])\1{2,}\s*$", '<hr class="md-rule">', t)  # --- → divider
    t = re.sub(r"(?m)^#{1,6}\s+(.*)$", r"<strong>\1</strong>", t)        # headings → bold
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", t)          # **bold**
    t = re.sub(r"(?m)^(\s*)[*\-]\s+", r"\1• ", t)                        # -/* bullets → •
    return Markup(t)


app.jinja_env.filters["mdlite"] = _md_lite


def _dtshort(iso):
    """Format a stored ISO timestamp as a short, readable Tasmanian-local string."""
    if not iso:
        return "Never"
    try:
        return datetime.fromisoformat(iso).astimezone(TAS).strftime("%d %b %Y, %H:%M")
    except (ValueError, TypeError):
        return str(iso)


app.jinja_env.filters["dtshort"] = _dtshort


def _strip_md(text):
    """Strip markdown markers for plain-text display (e.g. history titles)."""
    t = re.sub(r"[*_`#>]+", "", str(text or ""))
    return re.sub(r"\s+", " ", t).strip()


app.jinja_env.filters["stripmd"] = _strip_md


# ── Run history (GCS-backed) ──────────────────────────────────────────────────
_HISTORY_FILE = "history.json"
_HISTORY_CAP = 1000


def _record_run(kind, title, output, prompt=None, supplied=None, meta=None):
    """Append an AI run to the GCS-backed history. Best-effort: never break the
    user flow if the write fails. Newest first, capped at _HISTORY_CAP.

    `title` is a short excerpt for the list view; `prompt` is the full
    question/input that produced the output, preserved for the detail view."""
    import uuid
    try:
        data = load_json(_HISTORY_FILE)
        if not isinstance(data, dict):
            data = {}
        runs = data.get("runs", [])
        runs.insert(0, {
            "id": uuid.uuid4().hex[:12],
            "ts": datetime.now(tz=TAS).isoformat(),
            "user": session.get("user") or "—",
            "kind": kind,
            "title": (title or "").strip()[:200] or "(untitled)",
            "prompt": (prompt if prompt is not None else (title or ""))[:20000],
            "output": output or "",
            "supplied": supplied or [],
            "meta": meta or {},
        })
        data["runs"] = runs[:_HISTORY_CAP]
        save_json(_HISTORY_FILE, data)
    except Exception as e:
        _log("history.error", error=str(e)[:200], kind=kind)


_HISTORY_KINDS = {"ask": "Ask Holly", "council": "Planning Council",
                  "review": "Proposal Review", "caselaw": "Case Review"}


# ── Gemini factory ────────────────────────────────────────────────────────────
# Single source of truth for the model. gemini-2.5-flash only. Responses are
# prose; always extract JSON via re.search(r'\{.*\}', text, re.DOTALL).

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY")

_COUNCIL_MODELS = {
    "gemini":  {"label": "Gemini 2.5 Flash",     "chairman": True},
    "groq":    {"label": "Llama 3.3 70B (Groq)", "chairman": False},
    "minimax": {"label": "MiniMax M2.1",          "chairman": False},
}

_COUNCIL_MEMBER_SYSTEM = (
    "You are a Tasmanian statutory planning specialist on a multi-expert council. "
    "Assess the planning question using ONLY the scheme clauses and TASCAT decisions in CONTEXT. "
    "Cite every claim to a clause ID or case citation. Do not invent standards or holdings. "
    "Be concise (300–400 words) and structured. "
    "End with: '" + CAVEAT + "'"
)

_COUNCIL_CHAIRMAN_PREAMBLE = (
    "You are Holly, acting as Chair of a Tasmanian planning assessment council. "
    "Synthesise the council members' assessments into a single definitive response for a statutory planner. "
    "Incorporate the strongest insights, resolve any disagreements, and cite clause IDs and TASCAT citations raised. "
    "End with: '" + CAVEAT + "'"
)


def _council_active_members():
    """Return {model_key: config} for models with configured API keys."""
    active = {}
    if GEMINI_API_KEY:
        active["gemini"] = _COUNCIL_MODELS["gemini"]
    if GROQ_API_KEY:
        active["groq"] = _COUNCIL_MODELS["groq"]
    if MINIMAX_API_KEY:
        active["minimax"] = _COUNCIL_MODELS["minimax"]
    return active


def _http_post_json(url: str, payload: dict, headers: dict, timeout: int = 60) -> dict:
    """POST JSON and return parsed JSON. On an HTTP error, surface the response
    body (providers explain *why* a 4xx happened there) instead of a bare code."""
    import urllib.request
    import urllib.error
    # Some provider APIs (Groq) sit behind Cloudflare, which 403s the default
    # "Python-urllib/x.y" agent as a bot (Cloudflare error 1010 — banned browser
    # signature). Send a normal browser UA so the request reaches the provider.
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0.0.0 Safari/537.36"),
            "Accept": "application/json",
            "Accept-Language": "en-AU,en;q=0.9",
            **headers,
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400].strip()
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body or '(no body)'}") from None


def _council_query_gemini(prompt: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    data = _http_post_json(url, {
        "contents": [{"parts": [{"text": prompt}]}],
        # gemini-2.5-flash is a thinking model and its reasoning tokens count
        # against maxOutputTokens — leaving thinking on truncated the visible
        # answer mid-sentence. Disable thinking and give the whole budget to the
        # answer (synthesis of already-reasoned opinions doesn't need it).
        "generationConfig": {"maxOutputTokens": 4096, "temperature": 0.7,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }, {})
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if text.strip():
        return text
    raise RuntimeError(f"Gemini returned no text (finishReason={cand.get('finishReason')})")


def _council_query_groq(prompt: str) -> str:
    data = _http_post_json("https://api.groq.com/openai/v1/chat/completions", {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1536,
        "temperature": 0.7,
    }, {"Authorization": f"Bearer {GROQ_API_KEY}"})
    return data["choices"][0]["message"]["content"]


def _council_query_minimax(prompt: str) -> str:
    # MiniMax via its **Anthropic-compatible** endpoint on api.minimax.io — the
    # exact host/format OpenClaw uses (api.minimaxi.chat was a separate, empty
    # wallet that 1008'd). The portal serves the M2.1/M2.5 family (no "M2.7");
    # M2.1 is OpenClaw's default — fast and non-reasoning, so the answer is a
    # clean text block. The endpoint follows the Anthropic convention: the key
    # goes in the X-Api-Key header (NOT Authorization: Bearer — that 401s with
    # "carry the API secret key in the 'X-Api-Key' field"). MM-API-Source
    # attributes the request to the MiniMax Coding Plan (OpenClaw sends this
    # header too); without it the call bills the empty wallet and 402s (1008).
    # Use this app's own source identity, matching the key registered for it.
    data = _http_post_json("https://api.minimax.io/anthropic/v1/messages", {
        "model": "MiniMax-M2.1",
        "max_tokens": 1536,
        "messages": [{"role": "user", "content": prompt}],
    }, {"X-Api-Key": MINIMAX_API_KEY,
        "anthropic-version": "2023-06-01",
        "MM-API-Source": "TasPlan"})
    # Anthropic Messages: content is a list of blocks; concatenate the text ones
    # (a reasoning model would also emit separate "thinking" blocks we skip).
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if text.strip():
        return text
    # Surface the error shape MiniMax/Anthropic returns when there's no content.
    err = data.get("error") or {}
    if err:
        raise RuntimeError(f"MiniMax {err.get('type')}: {err.get('message')}")
    base = data.get("base_resp") or {}
    if base.get("status_code"):
        raise RuntimeError(f"MiniMax status_code={base.get('status_code')} "
                           f"status_msg={base.get('status_msg')!r}")
    raise ValueError(f"Unexpected MiniMax response: {list(data.keys())}")


def _council_query(model_key: str, prompt: str) -> str:
    if model_key == "gemini":
        return _council_query_gemini(prompt)
    if model_key == "groq":
        return _council_query_groq(prompt)
    if model_key == "minimax":
        return _council_query_minimax(prompt)
    raise ValueError(f"Unknown council model: {model_key}")


_GEMINI_SYSTEM = (
    "You are a Tasmanian town-planning assessment assistant. You review a "
    "development proposal against the Tasmanian Planning Scheme and relevant "
    "tribunal precedent. You are an analytical aid for qualified planners — NOT "
    "a statutory determination and NOT legal advice.\n\n"
    "GROUNDING RULES (non-negotiable):\n"
    "- Use ONLY the scheme clauses and tribunal decisions supplied in CONTEXT.\n"
    "- Cite every finding to a supplied clause ID or decision citation. Never "
    "cite anything not in CONTEXT. Never invent clause numbers, standards, or holdings.\n"
    "- If the supplied context is insufficient to assess a matter, say so "
    "explicitly and list what additional clause/information is needed. Do not guess.\n\n"
    "Return a SINGLE JSON object only (no prose outside it) with exactly these keys: "
    "municipality, applicable_zone, triggered_codes (array), use_classification "
    "(one of Permitted|Discretionary|Prohibited|No Permit Required), pathway_basis "
    "(array of {finding, clause_id}), standards_assessment (array of {standard, "
    "clause_id, acceptable_solution_met (bool), performance_criterion_note, status "
    "(compliant|gap|insufficient_info)}), compliance_gaps (array), relevant_precedents "
    "(array of {citation, relevance, outcome, principle}), risk_rating "
    "(low|moderate|high), recommended_conditions_or_info (array), caveat, "
    "context_sufficiency (sufficient|partial|insufficient)."
)


def _gemini_model(system=None):
    """Single Gemini factory. Returns a wrapper with .generate_content(prompt) -> response."""
    if not GEMINI_API_KEY:
        return None
    instruction = _GEMINI_SYSTEM if system is None else (system or None)

    class _Response:
        def __init__(self, text):
            self.text = text

    class _Wrapper:
        def generate_content(self, prompt, images=None):
            import urllib.request as _ur
            # Multimodal: image parts (inlineData) come before the text prompt.
            parts = (list(images) if images else []) + [{"text": prompt}]
            payload = {"contents": [{"parts": parts}],
                       # Disable thinking: gemini-2.5-flash counts reasoning tokens
                       # against the output budget and was truncating long answers.
                       "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.7,
                                            "thinkingConfig": {"thinkingBudget": 0}}}
            if instruction:
                payload["system_instruction"] = {"parts": [{"text": instruction}]}
            req = _ur.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with _ur.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return _Response(text)

    return _Wrapper()


def _skills_context():
    """Return a compact skills-framework string for inclusion in Gemini prompts."""
    data = load_json("skills.json")
    lines = ["Planner competency framework (audience context):"]
    for s in data.get("skills", []):
        titles = ", ".join(c["title"] for c in s.get("competencies", []))
        lines.append(f"  {s['number']}. {s['title']}: {titles}.")
    return "\n".join(lines)


# ── Retrieval ─────────────────────────────────────────────────────────────────

_STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "at", "with",
    "is", "are", "be", "as", "by", "from", "this", "that", "it", "use", "used",
    "development", "proposed", "proposal", "land", "site", "new",
}


def _tokens(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOPWORDS and len(w) > 2]


def _score_chunk(query_tokens, chunk):
    """Keyword overlap score against a chunk's keywords/title/text."""
    hay = " ".join([
        " ".join(chunk.get("keywords", [])),
        chunk.get("title", ""),
        chunk.get("zone_or_code", ""),
        chunk.get("text", ""),
        " ".join(chunk.get("use_classes", [])),
    ]).lower()
    score = 0
    for t in set(query_tokens):
        if t in chunk.get("keywords", []):
            score += 3          # exact keyword hit weighs most
        elif t in hay:
            score += 1
    return score


# ── Semantic retrieval (embeddings) ───────────────────────────────────────────

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768  # gemini-embedding-001 supports 768/1536/3072; 768 keeps the index compact


def _embed_text(text, task_type="RETRIEVAL_QUERY", raise_on_error=False):
    """Embed text via Gemini's free embedding endpoint. Returns a vector, or
    None when no key is set or the call fails. Same urllib pattern as the
    council helpers — no SDK dependency.

    The app path swallows errors (returns None) so retrieval degrades gracefully.
    The ingest path passes raise_on_error=True to surface the real cause."""
    if not GEMINI_API_KEY:
        if raise_on_error:
            raise RuntimeError("GEMINI_API_KEY is not set in this process's environment")
        return None
    import urllib.request
    import urllib.error
    payload = json.dumps({
        "model": f"models/{EMBED_MODEL}",
        "content": {"parts": [{"text": text[:8000]}]},
        "taskType": task_type,
        "outputDimensionality": EMBED_DIM,
    }).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{EMBED_MODEL}:embedContent?key={GEMINI_API_KEY}",
        data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data["embedding"]["values"]
    except urllib.error.HTTPError as e:
        if raise_on_error:
            body = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"HTTP {e.code} from embedding API: {body}") from None
        return None
    except Exception:
        if raise_on_error:
            raise
        return None


def _cosine(a, b):
    """Cosine similarity between two equal-length vectors. Pure stdlib."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def retrieve_passages(query, k=6, min_score=0.45):
    """Return top-k decision passages by cosine similarity to the query.

    Reads decision_chunks.json (full-text holding passages + embeddings). Returns
    [] when the file is absent or the query can't be embedded — callers then fall
    back to keyword-matched summaries, preserving graceful degradation."""
    chunks = load_json("decision_chunks.json").get("chunks", [])
    if not chunks:
        return []
    qvec = _embed_text(query, task_type="RETRIEVAL_QUERY")
    if not qvec:
        return []
    scored = []
    for c in chunks:
        emb = c.get("embedding")
        if not emb:
            continue
        s = _cosine(qvec, emb)
        if s >= min_score:
            scored.append((s, c))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [{**c, "_score": round(s, 3)} for s, c in scored[:k]]


# ── Zone- and municipality-aware retrieval ─────────────────────────────────────
# SPP zone numbers are fixed by statute. A question that names a zone must pull
# THAT zone's clauses (Inner Residential = clause 9.x), never a look-alike standard
# from another zone (e.g. Rural Living 11.x). Longest phrase wins so
# "inner residential" beats the bare "residential" / "rural" substrings.
_SPP_ZONES = {
    "general residential": 8, "inner residential": 9, "low density residential": 10,
    "rural living": 11, "village": 12, "urban mixed use": 13, "local business": 14,
    "general business": 15, "central business": 16, "commercial": 17,
    "light industrial": 18, "general industrial": 19, "rural": 20, "agriculture": 21,
    "landscape conservation": 22, "environmental management": 23, "major tourism": 24,
    "port and marine": 25, "utilities": 26, "community purpose": 27, "recreation": 28,
    "open space": 29, "future urban": 30,
}

# The 29 LPS councils, so /ask can scope local provisions from a free-text question.
_COUNCILS = [
    "Brighton", "Tasman", "Derwent Valley", "George Town", "Burnie",
    "Glamorgan Spring Bay", "Launceston", "Kingborough", "Central Coast",
    "Northern Midlands", "Dorset", "Circular Head", "Flinders", "Latrobe",
    "Glenorchy", "West Coast", "Sorell", "Central Highlands", "Clarence",
    "Southern Midlands", "Huon Valley", "Devonport", "West Tamar", "Wynyard",
    "Hobart", "Meander Valley", "Break O'Day", "Kentish", "King Island",
]


def _detect_zone(text):
    """Return the SPP zone number named in text (longest phrase match), or None."""
    t = (text or "").lower()
    for phrase in sorted(_SPP_ZONES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(phrase) + r"\b", t):
            return _SPP_ZONES[phrase]
    return None


def _detect_municipality(text):
    """Return the council named in text, or None (used to scope LPS on /ask)."""
    t = (text or "").lower()
    for name in sorted(_COUNCILS, key=len, reverse=True):
        if name.lower() in t:
            return name
    return None


def _zone_bonus(chunk, zone_num):
    """Strong boost for SPP clauses belonging to the queried zone (clause N.x)."""
    if not zone_num:
        return 0
    return 6 if re.match(rf"^SPP {zone_num}(\.|\b)", chunk.get("clause_id", "")) else 0


def _log(event, **fields):
    """Structured stdout log line — Cloud Run captures stdout per line."""
    try:
        print(json.dumps({"event": event, **fields}, default=str), flush=True)
    except Exception:
        pass


# ── Curated supplement ────────────────────────────────────────────────────────
# Authoritative scheme provisions bundled WITH THE APP so they are ALWAYS available
# to retrieval, independent of GCS ingestion. This is the backstop for standards the
# PDF chunker tends to lose (table-based density, parking, multiple-dwelling and
# private-open-space provisions). Each entry uses the scheme_chunks.json schema.
# Populate ONLY with verbatim official scheme text — never paraphrase or invent.
# See docs/INGESTION.md ("Curated supplement").
_SUPPLEMENT_CHUNKS = [
    # Example shape (do not ship placeholders — replace with real, verbatim text):
    # {"clause_id": "SPP 9.4.1", "instrument": "SPP", "scope": "statewide",
    #  "kind": "standard", "zone_or_code": "Inner Residential Zone",
    #  "title": "Density for multiple dwellings",
    #  "text": "<verbatim Acceptable Solutions and Performance Criteria>",
    #  "keywords": ["density", "multiple", "dwelling", "site", "area"],
    #  "use_classes": [], "provenance": "LIVE"},
]


def _load_scheme_chunks():
    """Ingested SPP + LPS clauses merged with the curated supplement. The
    supplement wins on a clause_id+scope clash, since it is hand-verified."""
    ingested = load_json("scheme_chunks.json").get("chunks", [])
    if not _SUPPLEMENT_CHUNKS:
        return ingested
    sup_keys = {(c.get("clause_id"), (c.get("scope") or "").lower()) for c in _SUPPLEMENT_CHUNKS}
    kept = [c for c in ingested
            if (c.get("clause_id"), (c.get("scope") or "").lower()) not in sup_keys]
    return kept + _SUPPLEMENT_CHUNKS


def retrieve(query, municipality=None, zone=None, use_class=None, k_scheme=8, k_decisions=4):
    """Return municipality-scoped scheme clauses + keyword-matched decisions.

    Scheme scope filter: a chunk is in-scope if it is statewide (SPP) or its
    scope matches the proposal's municipality (LPS). Decisions are ranked by
    keyword overlap with a boost for a municipality match. When a semantic index
    (decision_chunks.json) is present, full-text precedent passages are also
    attached for richer grounding.
    """
    qtext = " ".join(str(x) for x in [query, zone, use_class] if x)
    qtokens = _tokens(qtext)
    muni = (municipality or "").strip().lower()
    zone_num = _detect_zone(qtext)

    scheme = _load_scheme_chunks()
    in_scope = []
    for c in scheme:
        scope = (c.get("scope") or "").lower()
        if scope == "statewide" or (muni and scope == muni):
            in_scope.append(c)
    # Score = keyword overlap + a strong bonus for clauses in the queried zone, so a
    # zone-specific question surfaces that zone's standards over look-alikes elsewhere.
    def scheme_score(c):
        return _score_chunk(qtokens, c) + _zone_bonus(c, zone_num)
    scored = sorted(in_scope, key=scheme_score, reverse=True)
    # Keep only chunks with at least one hit; if none hit, fall back to scope-only.
    hits = [c for c in scored if scheme_score(c) > 0]
    scheme_out = (hits or scored)[:k_scheme]

    decisions = load_json("decisions.json").get("decisions", [])
    def dec_score(d):
        s = _score_chunk(qtokens, d)
        if muni and (d.get("municipality", "").lower() == muni):
            s += 2
        return s
    dec_sorted = sorted(decisions, key=dec_score, reverse=True)
    dec_out = [d for d in dec_sorted if dec_score(d) > 0][:k_decisions]

    # Semantic precedent passages (full holding text) when the index exists.
    passages = retrieve_passages(qtext)

    return {"scheme": scheme_out, "decisions": dec_out, "decision_passages": passages}


# ── Review engine ─────────────────────────────────────────────────────────────

# Keywords that, if present in the proposal, suggest a code/overlay is triggered.
_CODE_TRIGGERS = {
    "bushfire": ["bushfire", "bal", "bushfire-prone", "fire"],
    "coastal": ["coastal", "erosion", "foreshore", "beach", "sea", "tidal"],
    "inundation": ["inundation", "flood", "flooding", "stormwater", "low-lying"],
    "waterway": ["waterway", "river", "creek", "riparian", "wetland", "rivulet"],
    "scenic": ["scenic", "skyline", "ridgeline", "view", "kunanyi", "mountain"],
    "heritage": ["heritage", "historic", "listed"],
    "landslip": ["landslip", "landslide", "slope", "steep"],
}


def _classify_use(use_class, use_table_chunk):
    """Infer classification of use_class from a use-table chunk's text.

    Returns (classification, clause_id) or (None, None) if undetermined.
    """
    if not use_class or not use_table_chunk:
        return None, None
    text = use_table_chunk.get("text", "").lower()
    uc = use_class.lower()
    if uc not in text:
        return None, None
    window = text[text.index(uc): text.index(uc) + 120]
    for label in ("no permit required", "prohibited", "discretionary", "permitted"):
        if label in window:
            return label.title().replace("Required", "Required"), use_table_chunk.get("clause_id")
    return None, None


def _heuristic_review(proposal, ctx):
    """Deterministic, retrieval-grounded assessment used when Gemini is unavailable.

    Conservative by design: it reports what the corpus supports and flags
    everything else as insufficient_info rather than guessing.
    """
    muni = proposal.get("municipality", "").strip()
    zone = proposal.get("zone", "").strip()
    use_class = proposal.get("use_class", "").strip()
    desc = proposal.get("description", "")
    scheme, decisions = ctx["scheme"], ctx["decisions"]

    blob = " ".join(_tokens(" ".join([zone, use_class, desc])))

    # Triggered codes — only those for which we actually retrieved a code chunk.
    retrieved_codes = {c["clause_id"]: c for c in scheme if c.get("kind") == "code"}
    triggered = []
    for label, kws in _CODE_TRIGGERS.items():
        if any(kw in blob for kw in kws):
            for cid, c in retrieved_codes.items():
                if label in " ".join(c.get("keywords", [])) or label in c.get("title", "").lower():
                    triggered.append(f"{c['zone_or_code']} ({cid})")
    triggered = sorted(set(triggered))

    # Use classification from the zone use table, if retrieved.
    use_table = next((c for c in scheme if c.get("kind") == "use_table"), None)
    classification, class_clause = _classify_use(use_class, use_table)

    pathway_basis = []
    zone_purpose = next((c for c in scheme if c.get("kind") == "zone_purpose"), None)
    if zone_purpose:
        pathway_basis.append({"finding": f"Zone purpose: {zone_purpose['zone_or_code']}.", "clause_id": zone_purpose["clause_id"]})
    if classification and class_clause:
        pathway_basis.append({"finding": f"'{use_class}' classified as {classification} under the zone Use Table.", "clause_id": class_clause})
    elif use_table:
        pathway_basis.append({"finding": f"Use class '{use_class or '(not supplied)'}' could not be matched against the supplied Use Table; classification unverified.", "clause_id": use_table["clause_id"]})

    # Standards — listed, but never marked compliant without plans to verify against.
    standards = []
    for c in scheme:
        if c.get("kind") == "standard":
            standards.append({
                "standard": c.get("title", ""),
                "clause_id": c.get("clause_id"),
                "acceptable_solution_met": False,
                "performance_criterion_note": c.get("performance_criterion", "Performance criterion not supplied in context."),
                "status": "insufficient_info",
            })

    gaps = []
    if not use_table:
        gaps.append("Zone Use Table for the applicable zone was not retrieved — confirm zone and supply the Use Table clause.")
    if not classification and use_table:
        gaps.append(f"Could not confirm the pathway for '{use_class or '(use class not supplied)'}' from the supplied Use Table.")
    for s in standards:
        gaps.append(f"{s['standard']} ({s['clause_id']}): acceptable solution cannot be verified without submitted plans/figures.")
    for t in triggered:
        gaps.append(f"Triggered code {t}: requires the relevant hazard report / management plan to assess against the performance criterion.")

    precedents = [{
        "citation": d["citation"],
        "relevance": f"{d.get('title','')} — keywords: {', '.join(d.get('keywords', [])[:5])}.",
        "outcome": d.get("outcome", ""),
        "principle": d.get("principle", ""),
    } for d in decisions]

    # Risk: discretionary + triggered hazards raise it.
    risk = "low"
    if classification == "Discretionary" or triggered:
        risk = "moderate"
    if classification == "Prohibited" or len(triggered) >= 2:
        risk = "high"

    conditions = []
    if triggered:
        conditions.append("Request hazard reports / certified management plans for each triggered code before determination.")
    if classification in (None, "Discretionary"):
        conditions.append("Treat as discretionary: assess against performance criteria with merit evidence (plans, shadow diagrams, reports as applicable).")
    conditions.append("Confirm applicable zone and overlays against the current LPS map for the PID before relying on this review.")

    # Corpus is SAMPLE → never claim 'sufficient'.
    sufficiency = "partial" if scheme else "insufficient"

    return {
        "municipality": muni or "(not supplied)",
        "applicable_zone": zone or "(not supplied — confirm against LPS)",
        "triggered_codes": triggered,
        "use_classification": classification or "Discretionary",
        "pathway_basis": pathway_basis,
        "standards_assessment": standards,
        "compliance_gaps": gaps,
        "relevant_precedents": precedents,
        "risk_rating": risk,
        "recommended_conditions_or_info": conditions,
        "caveat": CAVEAT,
        "context_sufficiency": sufficiency,
    }


def _format_context(ctx):
    """Render retrieved context as a compact text block for the model prompt."""
    lines = ["SCHEME CLAUSES:"]
    for c in ctx["scheme"]:
        lines.append(f"- [{c['clause_id']}] {c.get('zone_or_code','')} — {c.get('title','')}: {c.get('text','')}"
                     + (f" || PERFORMANCE: {c['performance_criterion']}" if c.get("performance_criterion") else ""))
    lines.append("\nTRIBUNAL DECISIONS:")
    for d in ctx["decisions"]:
        lines.append(f"- {d['citation']} ({d.get('municipality','')}) [{d.get('outcome','')}]: {d.get('summary','')} PRINCIPLE: {d.get('principle','')}")
    passages = ctx.get("decision_passages") or []
    if passages:
        lines.append("\nKEY PRECEDENT PASSAGES (verbatim — cite the citation when relying on these):")
        for p in passages:
            lines.append(f"- {p['citation']} ({p.get('municipality','')}): {p.get('text','')}")
    return "\n".join(lines)


def review_proposal(proposal):
    """Assess a proposal and return the mandated JSON object (as a dict).

    Retrieves municipality-scoped context, then either asks Gemini (grounded on
    that context) or falls back to the deterministic heuristic. Either way the
    returned dict matches the mandated schema and carries the caveat.
    """
    ctx = retrieve(
        query=proposal.get("description", ""),
        municipality=proposal.get("municipality"),
        zone=proposal.get("zone"),
        use_class=proposal.get("use_class"),
    )

    model = _gemini_model()
    if model is not None:
        prompt = (
            f"PROPOSAL:\n{json.dumps(proposal, indent=2)}\n\n"
            f"CONTEXT (the ONLY material you may cite):\n{_format_context(ctx)}\n\n"
            "Produce the assessment as a single JSON object per your instructions."
        )
        try:
            response = model.generate_content(prompt)
            match = re.search(r"\{.*\}", response.text.strip(), re.DOTALL)
            result = json.loads(match.group() if match else response.text)
            result.setdefault("caveat", CAVEAT)
            return result, ctx, "gemini"
        except Exception:
            # Any failure → fall back to the grounded heuristic rather than error out.
            pass

    return _heuristic_review(proposal, ctx), ctx, "heuristic"


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def home():
    scheme = _load_scheme_chunks()
    decisions = load_json("decisions.json").get("decisions", [])
    return render_template("home.html", scheme_count=len(scheme), decision_count=len(decisions))


@app.route("/scheme")
@login_required
def scheme():
    chunks = _load_scheme_chunks()
    q = request.args.get("q", "").strip()
    muni = request.args.get("municipality", "").strip()
    if muni:
        chunks = [c for c in chunks if (c.get("scope", "").lower() in ("statewide", muni.lower()))]
    if q:
        qt = _tokens(q)
        chunks = [c for c in chunks if _score_chunk(qt, c) > 0]
    municipalities = sorted({c.get("scope") for c in _load_scheme_chunks() if c.get("scope") != "statewide"})
    manifest = load_json("scheme_manifest.json")
    return render_template("scheme.html", chunks=chunks, q=q, municipality=muni,
                           municipalities=municipalities, manifest=manifest)


@app.route("/decisions")
@login_required
def decisions():
    items = load_json("decisions.json").get("decisions", [])
    q = request.args.get("q", "").strip()
    if q:
        qt = _tokens(q)
        items = sorted([d for d in items if _score_chunk(qt, d) > 0],
                       key=lambda d: _score_chunk(qt, d), reverse=True)
    return render_template("decisions.html", decisions=items, q=q)


@app.route("/review", methods=["GET", "POST"])
@login_required
def review():
    result = ctx = engine = None
    proposal = {}
    if request.method == "POST":
        proposal = {
            "municipality": request.form.get("municipality", "").strip(),
            "address_pid": request.form.get("address_pid", "").strip(),
            "zone": request.form.get("zone", "").strip(),
            "use_class": request.form.get("use_class", "").strip(),
            "description": request.form.get("description", "").strip(),
        }
        if not proposal["description"] and not proposal["use_class"]:
            flash("Provide at least a proposed use class or a description to assess.", "danger")
        else:
            result, ctx, engine = review_proposal(proposal)
    return render_template("review.html", result=result, ctx=ctx, engine=engine,
                           proposal=proposal, gemini=bool(GEMINI_API_KEY))


@app.route("/api/review", methods=["POST"])
@login_required
def api_review():
    proposal = request.get_json(silent=True) or {}
    if not proposal.get("description") and not proposal.get("use_class"):
        return jsonify({"error": "Provide at least use_class or description."}), 400
    result, _ctx, engine = review_proposal(proposal)
    _title = (proposal.get("description") or
              " ".join(str(proposal.get(k, "")) for k in ("municipality", "zone", "use_class")).strip())
    _prompt = "\n".join(f"{k.replace('_', ' ').title()}: {v}"
                        for k in ("municipality", "pid", "zone", "use_class", "description")
                        if (v := str(proposal.get(k, "")).strip()))
    _record_run("review", _title, json.dumps(result, indent=2), prompt=_prompt,
                meta={"engine": engine, "proposal": proposal})
    return jsonify({"engine": engine, "result": result})


_HOLLY_SYSTEM = (
    "You are Holly, a Tasmanian planning specialist assistant embedded in TasPlan Review. "
    "Your audience is qualified statutory planners with competencies in scheme interpretation, "
    "development assessment, conditions drafting, referral management, and LUPAA procedure. "
    "Answer questions about the Tasmanian Planning Scheme, LUPAA, TASCAT decisions, and "
    "development assessment practice — but ONLY based on the CONTEXT supplied. "
    "Cite every claim to a clause ID or TASCAT citation from that context. "
    "When a question names a zone, rely on that zone's clauses and state which zone each "
    "cited clause governs; never apply a standard from a different zone. "
    "If the context is insufficient, say so clearly and explain what information is needed. "
    "Never invent clause numbers, standards, or case holdings. "
    "You may also receive ADDITIONAL CONTEXT SUPPLIED BY THE PLANNER. Treat any facts there "
    "as established facts about the proposal. If it includes clause or standard text the "
    "planner has pasted, you may rely on and quote it, but you MUST attribute it explicitly as "
    "'(planner-supplied)' and keep it distinct from the ingested scheme corpus — never present "
    "planner-supplied text as if it were a verified corpus citation, and still never invent anything. "
    "End every response with the caveat: '" + CAVEAT + "'"
)
_HOLLY_SYSTEM += (
    " If you state a date (e.g. a report date), use the current date supplied in the "
    "prompt — never invent, guess, or use a date from your training data."
)
_HOLLY_SYSTEM += (
    " The planner may attach images — site plans, architectural drawings, survey plans, or "
    "map/aerial screenshots. When images are present, FIRST report what you can actually "
    "observe under a short 'Image observations' heading: dimensions, scale bars, lot "
    "boundaries, setbacks, labels, north arrows, number of storeys or dwellings. Mark anything "
    "you infer or scale off without a stated scale bar explicitly as an ESTIMATE, and say "
    "plainly when something is not legible or no scale is shown — never guess a measurement. "
    "DO NOT merely list individual boundary segments: where a site plan shows boundary "
    "dimensions and/or a scale bar, DEDUCE the overall site — give each boundary's length, the "
    "approximate overall site dimensions (e.g. frontage x depth), and the approximate total "
    "site area (m2). State your method (summing labelled segments, or scaling against the scale "
    "bar) and any assumptions, and flag the derived figures as an ESTIMATE. If part of the "
    "boundary is illegible, give a best-estimate range and say what is missing rather than "
    "omitting the overall figure. Provide a one-line 'Site summary' (overall dimensions + area). "
    "Treat image-derived facts exactly like planner-supplied facts: they are inputs to the "
    "assessment, attributed as '(image-derived)', and are NOT corpus citations. Then assess the "
    "question against the ingested scheme clauses and TASCAT decisions as usual."
)
_HOLLY_SYSTEM += (
    " When the planner asks about subdivision potential or the smallest/minimum compliant "
    "subdivision of a site, add a 'Minimum compliant subdivision' section: "
    "(1) state the site dimensions/area you are working from (image-derived or planner-supplied, "
    "with the ESTIMATE/attribution rules above); "
    "(2) list EVERY applicable standard found in CONTEXT — minimum lot size, minimum frontage, "
    "front/side/rear setbacks, private open space, site coverage / building envelope, and access "
    "(including any battle-axe access-strip width) — and cite each to its clause ID; "
    "(3) using ONLY those cited standards, derive the minimum complying lot size(s), the required "
    "frontage, the buildable envelope remaining after setbacks, and the indicative maximum number "
    "of lots, SHOWING YOUR ARITHMETIC step by step; "
    "(4) call out the binding constraint (the standard that limits yield) and any hazard/overlay "
    "in CONTEXT that further constrains it. "
    "CRITICAL: if a standard you need is NOT in CONTEXT, do NOT invent a figure — say which "
    "standard/clause is missing and what value is required, so the planner can supply it. "
    "Present all lot sizes, setbacks and yields as INDICATIVE estimates that a licensed surveyor "
    "and the planning authority must confirm — this is a feasibility indication, not a sealed plan."
)

_CASELAW_SYSTEM = (
    "You are a Tasmanian planning law analyst. Analyse a planning tribunal decision and "
    "return a SINGLE JSON object with exactly these keys: "
    "case_name (string), citation (string), date (string), jurisdiction (string), "
    "primary_subject (string), decision_outcome (one of: Upheld|Set aside|Modified|Refused|Permit granted|Remitted), "
    "executive_summary ({core_issue, key_takeaway}), "
    "statutory_framework ({relevant_act, planning_scheme_zone, overlays}), "
    "factual_background ({proposal, authority_decision, grounds_for_appeal}), "
    "tribunal_findings ({scheme_interpretation, discretionary_powers, public_interests}), "
    "precedent ({test_applied, local_application}), "
    "implications ({for_applicants, for_authorities}), "
    "status ({appeal_window, recommended_actions}). "
    "Ground every field in the decision text supplied. If a field cannot be determined, use null. "
    "Focus on LUPAA, the Tasmanian Planning Scheme, and TASCAT/TASRMPAT procedure. "
    "Return JSON only — no prose outside the object."
)


_FOLLOWUP_SYSTEM = (
    "You help a Tasmanian planning assistant gather the information it is missing. "
    "Given a planning question and the assistant's assessment, identify the specific things "
    "that—if the planner provided them—would let the assistant give a more definitive, "
    "better-grounded answer. Prefer concrete proposal/site facts (e.g. lot frontage width, "
    "site slope, number of dwellings, parking spaces provided, lot area). Where the assessment "
    "stated a clause or standard was missing from its corpus, include a question inviting the "
    "planner to paste that specific clause's text. Each question must be specific and answerable "
    "in a sentence or a pasted clause. Return ONLY a JSON array of question strings, max 6, no prose."
)

_INSUFFICIENT_MARKERS = (
    "insufficient", "is insufficient", "not provided in", "context does not",
    "not been provided", "unable to determine", "not provided in the context",
)


def _is_insufficient(answer):
    a = (answer or "").lower()
    return any(m in a for m in _INSUFFICIENT_MARKERS)


def _holly_followups(question, answer):
    """Ask Gemini for specific follow-up questions that would sharpen the answer."""
    model = _gemini_model(system=_FOLLOWUP_SYSTEM)
    if not model:
        return []
    try:
        resp = model.generate_content(
            f"QUESTION:\n{question}\n\nASSESSMENT:\n{(answer or '')[:6000]}\n\nReturn the JSON array.")
        m = re.search(r"\[.*\]", resp.text, re.DOTALL)
        if not m:
            return []
        items = json.loads(m.group())
        return [str(q).strip() for q in items if str(q).strip()][:6]
    except Exception as e:
        _log("ask.followup_error", error=str(e)[:200])
        return []


def _pdf_safe(s):
    """Map common non-Latin-1 glyphs to ASCII so fpdf2 core fonts can render them."""
    repl = {"•": "-", "–": "-", "—": "-", "‘": "'", "’": "'",
            "“": '"', "”": '"', "…": "...", "→": "->", "≥": ">=",
            "≤": "<=", " ": " "}
    s = "".join(repl.get(ch, ch) for ch in str(s or ""))
    return s.encode("latin-1", "replace").decode("latin-1")


def _pdf_cell(pdf, h, text, **kw):
    """multi_cell that always advances to the next line at the left margin."""
    pdf.multi_cell(0, h, text, new_x="LMARGIN", new_y="NEXT", **kw)


def _pdf_render_markdown(pdf, text):
    """Render Holly's markdown-ish answer into the PDF, line by line."""
    for raw in (text or "").split("\n"):
        s = raw.strip()
        if not s:
            pdf.ln(2)
            continue
        if re.match(r"^([-*_])\1{2,}$", s):                       # --- divider
            y = pdf.get_y() + 1
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(3)
        elif re.match(r"^#{1,6}\s+", s):                          # heading
            pdf.set_font("Helvetica", "B", 11)
            _pdf_cell(pdf, 6, _pdf_safe(re.sub(r"^#{1,6}\s+", "", s)))
            pdf.set_font("Helvetica", "", 10)
        elif re.match(r"^[-*]\s+", s):                            # bullet
            pdf.set_font("Helvetica", "", 10)
            pdf.set_x(pdf.l_margin + 5)
            _pdf_cell(pdf, 5, _pdf_safe("- " + re.sub(r"^[-*]\s+", "", s)), markdown=True)
        else:                                                     # paragraph
            pdf.set_font("Helvetica", "", 10)
            _pdf_cell(pdf, 5, _pdf_safe(s), markdown=True)


def _report_pdf(question, answer, supplied=None):
    """Build a downloadable PDF of a Holly assessment. fpdf2 is pure-Python."""
    from fpdf import FPDF
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    _pdf_cell(pdf, 8, "TasPlan Review - Holly Assessment")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(110)
    _pdf_cell(pdf, 5, _pdf_safe("Generated " + datetime.now(tz=TAS).strftime("%d %b %Y, %H:%M")))
    pdf.set_text_color(0)
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 11)
    _pdf_cell(pdf, 6, "Question")
    pdf.set_font("Helvetica", "", 10)
    _pdf_cell(pdf, 5, _pdf_safe(question))
    pdf.ln(2)
    if supplied:
        pdf.set_font("Helvetica", "B", 11)
        _pdf_cell(pdf, 6, "Planner-supplied context")
        pdf.set_font("Helvetica", "", 10)
        for p in supplied:
            _pdf_cell(pdf, 5, _pdf_safe("Q: " + p.get("q", "")))
            _pdf_cell(pdf, 5, _pdf_safe("A: " + p.get("a", "")))
            pdf.ln(1)
    pdf.ln(1)
    pdf.set_font("Helvetica", "B", 12)
    _pdf_cell(pdf, 7, "Assessment")
    pdf.ln(1)
    _pdf_render_markdown(pdf, answer)
    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(110)
    _pdf_cell(pdf, 4, _pdf_safe(CAVEAT))
    return bytes(pdf.output())


@app.route("/skills")
@login_required
def skills():
    content = load_json("skills.json")
    return render_template("skills.html", content=content)


def _collect_supplied():
    """Merge previously-supplied context (carried as JSON) with any newly-answered
    follow-up questions from the refine form. Returns a list of {q, a} dicts."""
    supplied = []
    try:
        prior = json.loads(request.form.get("supplied") or "[]")
        if isinstance(prior, list):
            supplied = [{"q": str(p.get("q", "")), "a": str(p.get("a", ""))}
                        for p in prior if isinstance(p, dict) and str(p.get("a", "")).strip()]
    except (ValueError, TypeError):
        supplied = []
    for q, a in zip(request.form.getlist("fu_question"), request.form.getlist("fu_answer")):
        if (a or "").strip():
            supplied.append({"q": (q or "").strip(), "a": a.strip()})
    return supplied


@app.route("/ask", methods=["GET", "POST"])
@login_required
def ask_holly():
    answer = None
    question = ""
    error = None
    follow_ups = []
    supplied = []
    image_refs = []
    if request.method == "POST":
        question = request.form.get("question", "").strip()
        supplied = _collect_supplied()
        image_refs = _collect_images()
        if question:
            t0 = time.time()
            muni = _detect_municipality(question)
            ctx = retrieve(query=question, municipality=muni, k_scheme=12)
            _log("ask.retrieve", question=question[:200], municipality=muni,
                 zone=_detect_zone(question), supplied=len(supplied),
                 scheme_clauses=[c.get("clause_id") for c in ctx["scheme"]],
                 decisions=[d.get("citation") for d in ctx["decisions"]],
                 passages=len(ctx.get("decision_passages") or []))
            model = _gemini_model(system=_HOLLY_SYSTEM)
            if model:
                supplied_block = ""
                if supplied:
                    supplied_block = (
                        "\n\nADDITIONAL CONTEXT SUPPLIED BY THE PLANNER "
                        "(facts are established; any clause text here is planner-supplied — "
                        "attribute it as '(planner-supplied)', distinct from the corpus):\n"
                        + "\n".join(f"- Q: {p['q']}\n  A: {p['a']}" for p in supplied))
                img_parts = _images_as_gemini_parts(image_refs)
                image_block = ""
                if img_parts:
                    image_block = (
                        f"\n\nThe planner has attached {len(img_parts)} image(s) below "
                        "(site plan/drawing/map). Read them per your instructions: report "
                        "observable facts under 'Image observations', flag estimates, and treat "
                        "any dimensions as image-derived planner input — never as corpus citations.")
                prompt = (
                    f"Today's date is {datetime.now(tz=TAS).strftime('%d %B %Y')}.\n\n"
                    f"{_skills_context()}\n\n"
                    f"CONTEXT (scheme clauses and decisions — cite only these):\n{_format_context(ctx)}"
                    f"{supplied_block}{image_block}\n\n"
                    f"QUESTION: {question}"
                )
                try:
                    resp = model.generate_content(prompt, images=img_parts)
                    answer = resp.text.strip()
                    # Persist immediately — before the (slower) follow-up step — so the
                    # answer survives in History even if the user navigates away.
                    _record_run("ask", question, answer, prompt=question, supplied=supplied,
                                meta={"municipality": muni, "zone": _detect_zone(question),
                                      "images": image_refs})
                    if _is_insufficient(answer):
                        follow_ups = _holly_followups(question, answer)
                    _log("ask.answer", chars=len(answer), supplied=len(supplied),
                         images=len(image_refs),
                         follow_ups=len(follow_ups), latency_s=round(time.time() - t0, 2))
                except Exception as e:
                    error = f"Gemini error: {e}"
                    _log("ask.error", error=str(e)[:300], latency_s=round(time.time() - t0, 2))
            else:
                error = "No Gemini API key configured — Holly requires Gemini to answer questions."
    return render_template("ask.html", question=question, answer=answer, error=error,
                           follow_ups=follow_ups, supplied=supplied,
                           supplied_json=json.dumps(supplied), images=image_refs,
                           images_json=json.dumps(image_refs), gemini=bool(GEMINI_API_KEY))


@app.route("/ask/pdf", methods=["POST"])
@login_required
def ask_pdf():
    question = request.form.get("question", "")
    answer = request.form.get("answer", "")
    try:
        supplied = json.loads(request.form.get("supplied") or "[]")
        if not isinstance(supplied, list):
            supplied = []
    except (ValueError, TypeError):
        supplied = []
    if not answer.strip():
        return redirect(url_for("ask_holly"))
    from flask import Response
    pdf = _report_pdf(question, answer, supplied)
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": 'attachment; filename="holly-assessment.pdf"'})


@app.route("/uploads/<name>")
@login_required
def serve_upload(name):
    """Serve a stored image upload. Strict name match prevents path traversal or
    serving anything but the random-keyed images we created."""
    if not _UPLOAD_NAME_RE.match(name):
        abort(404)
    raw = load_bytes(_UPLOAD_PREFIX + name)
    if raw is None:
        abort(404)
    ext = name.rsplit(".", 1)[-1]
    mime = "image/jpeg" if ext == "jpg" else f"image/{ext}"
    return Response(raw, mimetype=mime, headers={"Cache-Control": "private, max-age=86400"})


@app.route("/council")
@login_required
def council():
    active = _council_active_members()
    return render_template("council.html", models=active, has_quorum=len(active) >= 2,
                           gemini=bool(GEMINI_API_KEY), groq=bool(GROQ_API_KEY),
                           minimax=bool(MINIMAX_API_KEY))


@app.route("/council/stream")
@login_required
def council_stream():
    question = request.args.get("q", "").strip()

    def generate():
        def sse(payload):
            return f"data: {json.dumps(payload)}\n\n"

        yield ": stream-open\n\n"   # prime the connection and flush headers immediately
        if not question:
            yield sse({"type": "error", "message": "No question provided."})
            return

        active = _council_active_members()
        if len(active) < 2:
            yield sse({"type": "error", "message": "Council requires at least 2 models. Configure GEMINI_API_KEY and GROQ_API_KEY."})
            return

        try:
            muni = _detect_municipality(question)
            ctx = retrieve(query=question, municipality=muni, k_scheme=12)
            skills = _skills_context()
            ctx_text = _format_context(ctx)
        except Exception as e:
            yield sse({"type": "error", "message": f"Failed to prepare context: {e}"})
            return
        _log("council.retrieve", question=question[:200], municipality=muni,
             zone=_detect_zone(question), members=list(active.keys()),
             scheme_clauses=[c.get("clause_id") for c in ctx["scheme"]],
             decisions=[d.get("citation") for d in ctx["decisions"]])
        member_prompt = (
            f"{_COUNCIL_MEMBER_SYSTEM}\n\n"
            f"{skills}\n\n"
            f"CONTEXT (cite only these):\n{ctx_text}\n\n"
            f"PLANNING QUESTION: {question}"
        )

        # Stage 1 — parallel first opinions
        yield sse({"type": "stage_start", "stage": 1, "message": "Gathering first opinions..."})
        stage1 = {}
        with ThreadPoolExecutor(max_workers=len(active)) as ex:
            futures = {ex.submit(_council_query, k, member_prompt): k for k in active}
            for fut in as_completed(futures):
                k = futures[fut]
                try:
                    resp = fut.result()
                except Exception as e:
                    resp = f"[Error: {e}]"
                if isinstance(resp, str) and resp.startswith("[Error"):
                    _log("council.member_error", stage=1, model=k, detail=resp[:300])
                stage1[k] = resp
                yield sse({"type": "stage1_response", "model": k,
                           "label": active[k]["label"], "response": resp})
        _log("council.stage_done", stage=1,
             chars={k: len(v) for k, v in stage1.items()})
        yield sse({"type": "stage_complete", "stage": 1})

        # Stage 2 — peer reviews (anonymised)
        yield sse({"type": "stage_start", "stage": 2, "message": "Processing peer reviews..."})
        anon = {f"Expert {i+1}": v for i, v in enumerate(stage1.values())}
        anon_text = "\n\n".join(f"{k}:\n{v}" for k, v in anon.items())
        review_prompt = (
            "You are a Tasmanian planning specialist reviewing peer assessments. "
            "Rank the following responses by: accuracy, completeness, and practical usefulness. "
            "Be specific and concise (200–300 words).\n\n"
            f"QUESTION: {question}\n\nPEER RESPONSES:\n{anon_text}\n\nYour ranking:"
        )
        stage2 = {}
        with ThreadPoolExecutor(max_workers=len(active)) as ex:
            futures = {ex.submit(_council_query, k, review_prompt): k for k in active}
            for fut in as_completed(futures):
                k = futures[fut]
                try:
                    rev = fut.result()
                except Exception as e:
                    rev = f"[Error: {e}]"
                if isinstance(rev, str) and rev.startswith("[Error"):
                    _log("council.member_error", stage=2, model=k, detail=rev[:300])
                stage2[k] = rev
                yield sse({"type": "stage2_review", "model": k,
                           "label": active[k]["label"], "review": rev})
        _log("council.stage_done", stage=2,
             chars={k: len(v) for k, v in stage2.items()})
        yield sse({"type": "stage_complete", "stage": 2})

        # Stage 3 — Holly (as Chair) synthesis (always Gemini)
        yield sse({"type": "stage_start", "stage": 3, "message": "Holly (as Chair) synthesising..."})
        s1_text = "\n\n".join(f"{active[k]['label']}:\n{v}" for k, v in stage1.items())
        s2_text = "\n\n".join(f"{active[k]['label']} review:\n{v}" for k, v in stage2.items())
        chairman_prompt = (
            f"{_COUNCIL_CHAIRMAN_PREAMBLE}\n\n"
            f"QUESTION: {question}\n\n"
            f"COUNCIL FIRST OPINIONS:\n{s1_text}\n\n"
            f"PEER REVIEWS:\n{s2_text}\n\n"
            "FINAL SYNTHESIS:"
        )
        try:
            final = _council_query_gemini(chairman_prompt)
        except Exception as e:
            final = f"[Synthesis error: {e}]"
            _log("council.synthesis_error", error=str(e)[:300])
        yield sse({"type": "stage3_final", "response": final})
        yield sse({"type": "stage_complete", "stage": 3})
        yield sse({"type": "council_complete"})
        _log("council.done", final_chars=len(final))   # server reached the end
        _record_run("council", question, final, prompt=question,
                    meta={"members": list(active.keys()),
                          "municipality": muni, "zone": _detect_zone(question)})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/caselaw", methods=["GET", "POST"])
@login_required
def caselaw():
    review = None
    citation = ""
    case_text = ""
    error = None
    if request.method == "POST":
        citation = request.form.get("citation", "").strip()
        case_text = request.form.get("case_text", "").strip()
        if not case_text:
            flash("Paste the decision text to analyse.", "warning")
        else:
            model = _gemini_model(system=_CASELAW_SYSTEM)
            if model:
                header = f"Citation: {citation}\n\n" if citation else ""
                prompt = (
                    f"{_skills_context()}\n\n"
                    f"DECISION TEXT:\n{header}{case_text[:20000]}"
                )
                try:
                    resp = model.generate_content(prompt)
                    match = re.search(r"\{.*\}", resp.text.strip(), re.DOTALL)
                    review = json.loads(match.group() if match else resp.text)
                    if citation and not review.get("citation"):
                        review["citation"] = citation
                    _record_run("caselaw",
                                review.get("case_name") or review.get("citation") or citation,
                                json.dumps(review, indent=2),
                                prompt=(f"Citation: {citation}\n\n" if citation else "") + case_text,
                                meta={"citation": review.get("citation") or citation})
                except Exception as e:
                    error = f"Analysis failed: {e}"
            else:
                error = "No Gemini API key configured — case analysis requires Gemini."
    return render_template("caselaw.html", review=review, citation=citation,
                           case_text=case_text, error=error, gemini=bool(GEMINI_API_KEY))


@app.route("/history")
@login_required
def history():
    runs = (load_json(_HISTORY_FILE) or {}).get("runs", [])
    q = request.args.get("q", "").strip()
    kind = request.args.get("kind", "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", 10)), 500))
    except (TypeError, ValueError):
        limit = 10
    filtered = runs
    if kind:
        filtered = [r for r in filtered if r.get("kind") == kind]
    if q:
        ql = q.lower()
        filtered = [r for r in filtered
                    if ql in (r.get("title", "") + " " + r.get("output", "") + " " + r.get("user", "")).lower()]
    total = len(filtered)
    return render_template("history.html", runs=filtered[:limit], total=total, limit=limit,
                           q=q, kind=kind, kinds=_HISTORY_KINDS, has_more=total > limit)


@app.route("/history/<rid>")
@login_required
def history_detail(rid):
    from flask import abort
    run = next((r for r in (load_json(_HISTORY_FILE) or {}).get("runs", []) if r.get("id") == rid), None)
    if not run:
        abort(404)
    return render_template("history_detail.html", run=run, kinds=_HISTORY_KINDS)


@app.route("/history/<rid>/pdf")
@login_required
def history_pdf(rid):
    from flask import abort, Response
    run = next((r for r in (load_json(_HISTORY_FILE) or {}).get("runs", []) if r.get("id") == rid), None)
    if not run:
        abort(404)
    pdf = _report_pdf(run.get("prompt") or run.get("title", ""),
                      run.get("output", ""), run.get("supplied"))
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="tasplan-{run.get("kind", "run")}-{rid}.pdf"'})


@app.route("/admin")
@admin_required
def admin():
    manifest = load_json("scheme_manifest.json")
    scheme = _load_scheme_chunks()
    decisions_data = load_json("decisions.json").get("decisions", [])
    users = load_json("users.json")
    return render_template("admin.html", manifest=manifest, scheme_count=len(scheme),
                           decision_count=len(decisions_data), users=users,
                           gemini=bool(GEMINI_API_KEY))


@app.route("/admin/users/add", methods=["POST"])
@admin_required
def admin_user_add():
    from werkzeug.security import generate_password_hash
    username = request.form.get("username", "").strip().lower()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    role = request.form.get("role", "user")
    password = request.form.get("password", "").strip()
    if not username or not password:
        flash("Username and password are required.", "warning")
        return redirect(url_for("admin"))
    users = load_json("users.json")
    if username in users:
        flash(f"Username '{username}' already exists.", "warning")
        return redirect(url_for("admin"))
    users[username] = {
        "name": name,
        "email": email,
        "role": role,
        "password": generate_password_hash(password),
        "created_at": datetime.now(TAS).isoformat(timespec="seconds"),
    }
    save_json("users.json", users)
    flash(f"User '{username}' created.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/users/edit", methods=["POST"])
@admin_required
def admin_user_edit():
    from werkzeug.security import generate_password_hash
    username = request.form.get("username", "").strip().lower()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    role = request.form.get("role", "user")
    password = request.form.get("password", "").strip()
    users = load_json("users.json")
    if username not in users:
        flash("User not found.", "warning")
        return redirect(url_for("admin"))
    users[username]["name"] = name
    users[username]["email"] = email
    users[username]["role"] = role
    if password:
        users[username]["password"] = generate_password_hash(password)
    save_json("users.json", users)
    flash(f"User '{username}' updated.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/users/delete", methods=["POST"])
@admin_required
def admin_user_delete():
    username = request.form.get("username", "").strip().lower()
    if username == session.get("user"):
        flash("Cannot delete your own account.", "warning")
        return redirect(url_for("admin"))
    users = load_json("users.json")
    if username in users:
        del users[username]
        save_json("users.json", users)
        flash(f"User '{username}' deleted.", "success")
    return redirect(url_for("admin"))


@app.errorhandler(403)
def err_403(e):
    return render_template("error.html", code=403, message="Forbidden"), 403


@app.errorhandler(404)
def err_404(e):
    return render_template("error.html", code=404, message="Not Found"), 404


@app.errorhandler(500)
def err_500(e):
    return render_template("error.html", code=500, message="Server Error"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
