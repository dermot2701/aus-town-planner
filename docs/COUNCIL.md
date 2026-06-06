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
| `minimax` | MiniMax M2.7 | Member | `MINIMAX_API_KEY` | `api.minimaxi.chat/v1/text/chatcompletion_v2` |

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
sits behind Cloudflare, whose WAF returns 1010 ("banned browser signature") for
requests that look like bots — including the default `Python-urllib/x.y`
User-Agent and requests with no `Accept-Language`.

**Fix (in `_http_post_json`):** send a normal browser `User-Agent`, `Accept`, and
`Accept-Language` on every council POST. Callers can still override any header;
`Content-Type: application/json` is always forced last.

If 1010 ever returns *despite* the browser headers, it's no longer UA-based — the
likely causes are an IP/ASN-level block on the Cloud Run egress region, or a
Cloudflare rule change. Levers then: redeploy in a different region, route the
Groq call through an egress proxy, or contact Groq. (A valid/invalid **key**
produces 401, not 1010, so a 1010 is never the key.)

### MiniMax → `status_code=1008 status_msg='insufficient balance'`

This appeared **even with ample subscription quota unused**. The cause was the
**model name**, not the balance:

- MiniMax separates a **subscription plan quota** (covers the current model
  family — M3 / M2.7 / image / speech / music) from a pay-as-you-go **credit
  balance**. Credits are only consumed *after* the plan quota is exhausted.
- We were calling the legacy **`MiniMax-Text-01`**, which is **not** in the plan's
  model family — so billing skipped the plan quota, fell to the credit balance
  (0), and rejected with `1008`.

**Fix:** call a plan-covered model. The app uses **`MiniMax-M2.7`** (the same
model coding-agent clients like OpenClaw use successfully on the same account).

> Model-string gotcha: gateways (OpenRouter / LiteLLM, and tools like OpenClaw)
> use a provider-prefixed name `minimax/MiniMax-M2.7`. MiniMax's **native** API
> (which this app calls directly) wants the **bare** `MiniMax-M2.7` — the
> `minimax/` prefix would be rejected as an unknown model.

MiniMax `base_resp.status_code` cheat-sheet: `1004` auth failed · `1008`
insufficient balance · `1002` rate limit · `1027` output content risk
(moderation).

### "Connection lost — please try again" (client)

Historically an infinite-recursion bug in `council.html` (two `handle()`
declarations) fired on the first SSE message. Fixed: a single `handle()`, plus a
non-destructive `onerror` that ignores the normal stream close after
`council_complete` and keeps partial results instead of wiping the page. If you
see this again, confirm the server reached `council.done` in the logs before
suspecting the client.

## Operational checklist

After any change to the council or its providers, convene it once and confirm:

- [ ] Each active member returns a Stage 1 opinion (no `council.member_error`).
- [ ] Stage 3 synthesis renders under **"Holly's Synthesis (Chair)"**.
- [ ] `council.done` appears in the logs with a non-trivial `final_chars`.
- [ ] Adding/removing a provider key changes the active-member list and the
      quorum warning as expected.
