# Planning Council — multi-model debate

The **Planning Council** (`/council`) answers a planning question by convening
several independent LLMs, having them critique each other, then synthesising a
single grounded answer. It is the most moving-parts feature in the app, and the
one most exposed to third-party provider quirks — this document is the operator's
guide to how it works and how to keep all members healthy.

> **Load-bearing caveat.** Like every other answer in the app, each member's
> opinion and the final synthesis carry the `CAVEAT` (*analytical aid only; not a
> statutory determination or legal advice*). Do not remove it.

## How it works

Three stages, streamed to the browser over Server-Sent Events (`/council/stream`,
`EventSource` on the client in `templates/council.html`):

1. **Stage 1 — First Opinions.** Every active member answers the question
   independently, grounded **only** on the retrieved scheme clauses + TASCAT
   decisions (same `retrieve()` the rest of the app uses, with zone/municipality
   detection). No cross-talk.
2. **Stage 2 — Peer Reviews.** Each member ranks the (anonymised) Stage 1
   answers for accuracy, completeness, and usefulness.
3. **Stage 3 — Synthesis.** **Holly, acting as Chair** (the Gemini model),
   merges the opinions and reviews into one definitive, cited response.

Members run concurrently per stage (`ThreadPoolExecutor`). A member that errors
does **not** abort the run — its tile shows the error and the others continue.
The server always reaches `council.done`; if the browser shows "Connection lost"
that's a client transport issue, not a stalled server (see the SSE notes in
`council.html`).

## Members & configuration

| Key | Label | Role | Env var | Endpoint |
|-----|-------|------|---------|----------|
| `gemini` | Gemini 2.5 Flash | Member + **Chair** (Stage 3) | `GEMINI_API_KEY` | `generativelanguage.googleapis.com` |
| `groq` | Llama 3.3 70B (Groq) | Member | `GROQ_API_KEY` | `api.groq.com/openai/v1` |
| `minimax` | MiniMax M2.1 | Member | `MINIMAX_API_KEY` | `api.minimax.io/anthropic/v1/messages` (Anthropic Messages; model `MiniMax-M2.1`) |

- A member is **active** only if its key is present. The council needs a
  **quorum of ≥2**; with fewer, the page shows a warning and disables the button.
- Keys are read from the environment (`os.environ`) — in production via Secret
  Manager. Never hardcode them.
- All three providers are called through one helper, **`_http_post_json`**, which
  is also where the shared request hardening lives (see below).

## Diagnosing a dead member

Every member failure is logged as a structured line you can search in Cloud Run:

```
{"event":"council.member_error","model":"groq","stage":1,"detail":"[Error: ...]"}
```

`_http_post_json` deliberately surfaces the **real** provider error rather than a
bare status code:

- On an HTTP error it reads the **response body** and includes the reason phrase:
  `HTTP 403 Forbidden: <body>` (falling back to `(no body)`).
- For MiniMax it surfaces `base_resp.status_code` / `status_msg` **and** the
  `input/output_sensitive` moderation flags when `choices` comes back empty —
  because MiniMax returns HTTP 200 with an empty `choices` array on a billing or
  moderation rejection, not an HTTP error.

If you ever see an opaque error, that's the first thing to restore — the whole
point is that the log names the cause.

## Known failure modes (and their fixes)

These all actually happened; keep the fixes in place.

### Groq → `HTTP 403 Forbidden: error code: 1010`

**`error code: 1010` is a Cloudflare block, not a Groq auth failure.** Groq's API
sits behind Cloudflare, whose WAF returns `1010` ("banned browser signature") for
requests that *look* like bots — most commonly a request whose `User-Agent` is the
language default (`Python-urllib/x.y`, `python-requests/x.y`, `Go-http-client`,
etc.) and/or that omits ordinary browser headers like `Accept-Language`. The
request is killed at Cloudflare's edge **before Groq's API ever sees it**.

**How to be sure it's an edge block (the diagnostic tell):** check the Groq
dashboard. If **Usage shows 0 API calls** and **Logs show nothing** while your app
gets a 403, the request never reached Groq — so it cannot be the key (a bad key
reaches Groq and is *logged* as a 401). That combination = Cloudflare edge block.

**Fix:** send a realistic browser `User-Agent` (plus `Accept` and
`Accept-Language`) on the request. In this app that lives in `_http_post_json`, so
every council POST gets it; callers can still override, and
`Content-Type: application/json` is forced last.

```python
headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-AU,en;q=0.9",
    "Authorization": f"Bearer {GROQ_API_KEY}",
    "Content-Type": "application/json",
}
```

After the fix, calls start appearing on the Groq Usage/Logs dashboard — that's the
confirmation it cleared Cloudflare.

If `1010` persists *despite* browser headers, it's no longer UA-based: suspect an
IP/ASN-level block on your server's egress (some cloud regions' ranges are
flagged), or a Cloudflare rule change. Levers then: deploy from a different
region, route the call through an egress proxy with a clean IP, or contact Groq.
A `1010` is **never** the key (that's a `401`).

> **Portable note (applies to any HTTP LLM client, e.g. another council app):**
> a `403 … 1010` from *any* Cloudflare-fronted API (Groq and others) almost always
> means "set a browser-like `User-Agent`." SDKs usually set one; raw
> `urllib`/`requests`/`fetch` from a server often don't. This is the first thing
> to try whenever a provider 403s with a Cloudflare error code and the dashboard
> shows zero received calls.

### MiniMax → `status_code=1008 status_msg='insufficient balance'`

This appeared on the **wrong host** (`api.minimaxi.chat`) **even with ample
subscription quota unused**, and **persisted after switching models** — which
ruled out the model. The real cause was the **API host**: the account,
subscription, and quota live on **`api.minimax.io`**, whereas `api.minimaxi.chat`
is a separate platform whose wallet was empty, so it `1008`'d no matter the model.

**Fix:** call MiniMax on the exact host/format that already works for this account
(this is how OpenClaw is configured), via the **Anthropic-compatible** endpoint:

- `POST https://api.minimax.io/anthropic/v1/messages`
- **`X-Api-Key: $MINIMAX_API_KEY`** — the Anthropic convention this endpoint
  follows. `Authorization: Bearer` is **rejected with `401`** ("carry the API
  secret key in the 'X-Api-Key' field"). Also send `anthropic-version: 2023-06-01`.
- **`MM-API-Source: TasPlan`** — attributes the request to the MiniMax **Coding
  Plan**. Without it the call bills the empty pay-as-you-go wallet and returns
  **`402 insufficient_balance (1008)`** even when the Coding Plan has quota. Set
  it to this app's registered source identity (OpenClaw sends `OpenClaw`); it
  should match the source the key was created under.
- body is Anthropic Messages: `{"model": "MiniMax-M2.1", "max_tokens": …, "messages": […]}`
- response `content` is a list of blocks — concatenate the `type:"text"` ones
  (a reasoning model like `MiniMax-M2.5` also emits `thinking` blocks, which we skip)

> **Model names:** the portal serves the **M2.1 / M2.5** family — there is **no
> `MiniMax-M2.7`** here (that slug only exists on OpenRouter). `MiniMax-M2.1` is
> the fast, non-reasoning default; `MiniMax-M2.5` is the reasoning model.

`base_resp.status_code` cheat-sheet (native OpenAI-format endpoint): `1004` auth
failed · `1008` insufficient balance · `1002` rate limit · `1027` output content
risk (moderation). A `1008` that survives a host/model check points at the wrong
account/host, not the model.

### "Connection lost — please try again" (client)

Historically an infinite-recursion bug in `council.html` (two `handle()`
declarations) fired on the first SSE message. Fixed: a single `handle()`, plus a
non-destructive `onerror` that ignores the normal stream close after
`council_complete` and keeps partial results instead of wiping the page. If you
see this again, confirm the server reached `council.done` in the logs before
suspecting the client.

## Page layout & client (SSE)

`templates/council.html` is the whole client. Layout and streaming details that
matter:

**Layout.** The page is a vertical stack, **not** a 2-column grid: a full-width
**question bar across the top** (textarea + a `Convene Council` button, with the
active members shown as chips), and the **full-width 3-stage output below**. This
was a deliberate change — the old 2-column layout gave answers only half the page
width, truncating long opinions. Output boxes are generous (`.stage-body`
`max-height:70vh`, `.council-final` `max-height:80vh`) so answers rarely need
inner scrolling.

**Token ceilings.** So answers aren't cut off server-side: Gemini
`maxOutputTokens` 2048, Groq/MiniMax `max_tokens` 1536. Bump these if you see
genuinely truncated output (as opposed to a too-small box).

**SSE event types** (server `yield sse({...})` → client `handle(ev)`):

| `type` | When | Client action |
|--------|------|----------------|
| `stage_start` | each stage begins | show the loading spinner for that stage |
| `stage1_response` | a member's first opinion | add/refresh its Stage 1 tab |
| `stage_complete` (stage 1/2/3) | a stage finishes | mark the stage number ✓, open the next shell |
| `stage2_review` | a member's peer review | add/refresh its Stage 2 tab |
| `stage3_final` | the Chair's synthesis | fill the final box |
| `council_complete` | run finished cleanly | set `completed=true`, close the stream |
| `error` | context prep failed | replace the panel with the error |

**Errored member never steals the default tab.** A failing member (its text starts
`[Error`) returns almost instantly, so it would otherwise arrive first and become
the selected Stage 1/2 tab — hiding the real answers behind it. `defaultKey()`
selects the first **non-error** response as the active tab.

**"Connection lost" is a client concern, not the server.** `onerror` ignores the
normal stream close that follows `council_complete`, and keeps partial results
rather than wiping the page. The single `handle()` function matters: an earlier
bug declared it twice, causing infinite recursion on the first message. Always
confirm the server reached `council.done` in the logs before suspecting the wire.

## Operational checklist

After any change to the council or its providers, convene it once and confirm:

- [ ] Each active member returns a Stage 1 opinion (no `council.member_error`).
- [ ] Stage 3 synthesis renders under **"Holly's Synthesis (Chair)"**.
- [ ] `council.done` appears in the logs with a non-trivial `final_chars`.
- [ ] Adding/removing a provider key changes the active-member list and the
      quorum warning as expected.
