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
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, abort,
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


# ── Gemini factory ────────────────────────────────────────────────────────────
# Single source of truth for the model. gemini-2.5-flash only. Responses are
# prose; always extract JSON via re.search(r'\{.*\}', text, re.DOTALL).

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

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


def _gemini_model():
    if not GEMINI_API_KEY:
        return None
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    return genai.GenerativeModel("gemini-2.5-flash", system_instruction=_GEMINI_SYSTEM)


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


def retrieve(query, municipality=None, zone=None, use_class=None, k_scheme=8, k_decisions=4):
    """Return municipality-scoped scheme clauses + keyword-matched decisions.

    Scheme scope filter: a chunk is in-scope if it is statewide (SPP) or its
    scope matches the proposal's municipality (LPS). Decisions are ranked by
    keyword overlap with a boost for a municipality match.
    """
    qtext = " ".join(str(x) for x in [query, zone, use_class] if x)
    qtokens = _tokens(qtext)
    muni = (municipality or "").strip().lower()

    scheme = load_json("scheme_chunks.json").get("chunks", [])
    in_scope = []
    for c in scheme:
        scope = (c.get("scope") or "").lower()
        if scope == "statewide" or (muni and scope == muni):
            in_scope.append(c)
    scored = sorted(in_scope, key=lambda c: _score_chunk(qtokens, c), reverse=True)
    # Keep only chunks with at least one hit; if none hit, fall back to scope-only.
    hits = [c for c in scored if _score_chunk(qtokens, c) > 0]
    scheme_out = (hits or scored)[:k_scheme]

    decisions = load_json("decisions.json").get("decisions", [])
    def dec_score(d):
        s = _score_chunk(qtokens, d)
        if muni and (d.get("municipality", "").lower() == muni):
            s += 2
        return s
    dec_sorted = sorted(decisions, key=dec_score, reverse=True)
    dec_out = [d for d in dec_sorted if dec_score(d) > 0][:k_decisions]

    return {"scheme": scheme_out, "decisions": dec_out}


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
    scheme = load_json("scheme_chunks.json").get("chunks", [])
    decisions = load_json("decisions.json").get("decisions", [])
    return render_template("home.html", scheme_count=len(scheme), decision_count=len(decisions))


@app.route("/scheme")
@login_required
def scheme():
    chunks = load_json("scheme_chunks.json").get("chunks", [])
    q = request.args.get("q", "").strip()
    muni = request.args.get("municipality", "").strip()
    if muni:
        chunks = [c for c in chunks if (c.get("scope", "").lower() in ("statewide", muni.lower()))]
    if q:
        qt = _tokens(q)
        chunks = [c for c in chunks if _score_chunk(qt, c) > 0]
    municipalities = sorted({c.get("scope") for c in load_json("scheme_chunks.json").get("chunks", []) if c.get("scope") != "statewide"})
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
    return jsonify({"engine": engine, "result": result})


@app.route("/admin")
@admin_required
def admin():
    manifest = load_json("scheme_manifest.json")
    scheme = load_json("scheme_chunks.json").get("chunks", [])
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
