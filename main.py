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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY")

_COUNCIL_MODELS = {
    "gemini":  {"label": "Gemini 2.5 Flash",     "chairman": True},
    "groq":    {"label": "Llama 3.3 70B (Groq)", "chairman": False},
    "minimax": {"label": "MiniMax-01",            "chairman": False},
}

_COUNCIL_MEMBER_SYSTEM = (
    "You are a Tasmanian statutory planning specialist on a multi-expert council. "
    "Assess the planning question using ONLY the scheme clauses and TASCAT decisions in CONTEXT. "
    "Cite every claim to a clause ID or case citation. Do not invent standards or holdings. "
    "Be concise (300–400 words) and structured. "
    "End with: '" + CAVEAT + "'"
)

_COUNCIL_CHAIRMAN_PREAMBLE = (
    "You are the Chairman of a Tasmanian planning assessment council. "
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


def _council_query_gemini(prompt: str) -> str:
    import urllib.request
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.7},
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _council_query_groq(prompt: str) -> str:
    import urllib.request
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = json.dumps({
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
        "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def _council_query_minimax(prompt: str) -> str:
    import urllib.request
    url = "https://api.minimaxi.chat/v1/text/chatcompletion_v2"
    payload = json.dumps({
        "model": "MiniMax-Text-01",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
        "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    if "choices" in data and data["choices"]:
        return data["choices"][0]["message"]["content"]
    if "reply" in data:
        return data["reply"]
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
    """Single Gemini factory. Defaults to the review system instruction.
    Pass system='holly', 'caselaw', or a string to use a different system prompt."""
    if not GEMINI_API_KEY:
        return None
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    instruction = _GEMINI_SYSTEM if system is None else (system or None)
    kwargs = {"system_instruction": instruction} if instruction else {}
    return genai.GenerativeModel("gemini-2.5-flash", **kwargs)


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


_HOLLY_SYSTEM = (
    "You are Holly, a Tasmanian planning specialist assistant embedded in TasPlan Review. "
    "Your audience is qualified statutory planners with competencies in scheme interpretation, "
    "development assessment, conditions drafting, referral management, and LUPAA procedure. "
    "Answer questions about the Tasmanian Planning Scheme, LUPAA, TASCAT decisions, and "
    "development assessment practice — but ONLY based on the CONTEXT supplied. "
    "Cite every claim to a clause ID or TASCAT citation from that context. "
    "If the context is insufficient, say so clearly and explain what information is needed. "
    "Never invent clause numbers, standards, or case holdings. "
    "End every response with the caveat: '" + CAVEAT + "'"
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


@app.route("/skills")
@login_required
def skills():
    content = load_json("skills.json")
    return render_template("skills.html", content=content)


@app.route("/ask", methods=["GET", "POST"])
@login_required
def ask_holly():
    answer = None
    question = ""
    error = None
    if request.method == "POST":
        question = request.form.get("question", "").strip()
        if question:
            ctx = retrieve(query=question)
            model = _gemini_model(system=_HOLLY_SYSTEM)
            if model:
                prompt = (
                    f"{_skills_context()}\n\n"
                    f"CONTEXT (scheme clauses and decisions — cite only these):\n{_format_context(ctx)}\n\n"
                    f"QUESTION: {question}"
                )
                try:
                    resp = model.generate_content(prompt)
                    answer = resp.text.strip()
                except Exception as e:
                    error = f"Gemini error: {e}"
            else:
                error = "No Gemini API key configured — Holly requires Gemini to answer questions."
    return render_template("ask.html", question=question, answer=answer, error=error,
                           gemini=bool(GEMINI_API_KEY))


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

        if not question:
            yield sse({"type": "error", "message": "No question provided."})
            return

        active = _council_active_members()
        if len(active) < 2:
            yield sse({"type": "error", "message": "Council requires at least 2 models. Configure GEMINI_API_KEY and GROQ_API_KEY."})
            return

        ctx = retrieve(query=question)
        skills = _skills_context()
        ctx_text = _format_context(ctx)
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
                stage1[k] = resp
                yield sse({"type": "stage1_response", "model": k,
                           "label": active[k]["label"], "response": resp})
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
                stage2[k] = rev
                yield sse({"type": "stage2_review", "model": k,
                           "label": active[k]["label"], "review": rev})
        yield sse({"type": "stage_complete", "stage": 2})

        # Stage 3 — chairman synthesis (always Gemini)
        yield sse({"type": "stage_start", "stage": 3, "message": "Chairman synthesising..."})
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
        yield sse({"type": "stage3_final", "response": final})
        yield sse({"type": "stage_complete", "stage": 3})
        yield sse({"type": "council_complete"})

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
                except Exception as e:
                    error = f"Analysis failed: {e}"
            else:
                error = "No Gemini API key configured — case analysis requires Gemini."
    return render_template("caselaw.html", review=review, citation=citation,
                           case_text=case_text, error=error, gemini=bool(GEMINI_API_KEY))


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
