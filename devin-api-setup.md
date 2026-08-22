# Devin API — setup

> **Status: key not yet available.** Everything below is written against the
> published Devin docs so the orchestrator can be built now and wired up the
> moment the key lands. Nothing here contains a real credential.

## What we need handed to us

1. A **service user API key** — starts with `cog_`. Created in *Settings → Service users*
   (org) or *Enterprise settings → Service users*. Shown once; copy on creation.
2. The **organization ID** — every v3 path is scoped to it.
3. An **ACU budget** for the event, so we can set `max_acu_limit` sensibly.

Ask the organizers for all three on day one.

## Which API version

Use **v3**. The v1 API (`/v1/sessions`) is marked deprecated in the docs in favour of
v3 with service-user authentication. Legacy `apk_user_` / `apk_` keys only work on
v1–v2 and don't support v3 features.

- Base URL: `https://api.devin.ai/v3`
- Auth header: `Authorization: Bearer cog_...`
- Org-scoped paths: `/v3/organizations/{ORG_ID}/...`

## Endpoints the orchestrator uses

| Purpose | Call |
|---|---|
| Create a session | `POST /v3/organizations/{ORG_ID}/sessions` |
| Poll a session | `GET /v3/organizations/{ORG_ID}/sessions/{SESSION_ID}` |
| Feed a verdict back in | `POST /v3/organizations/{ORG_ID}/sessions/{SESSION_ID}/messages` |
| List sessions | `GET /v3/organizations/{ORG_ID}/sessions` |

Terminal statuses when polling: `exit` (completed), `error`, `suspended`.
Docs suggest polling roughly every 10s.

Create-session body fields worth using for this project:

| Field | Why we care |
|---|---|
| `prompt` | case ID, mesh URI, constraint spec, verifier contract |
| `snapshot_id` | the machine image with CadQuery/gmsh/CalculiX pre-baked — avoids burning ten minutes per session on installs |
| `max_acu_limit` | hard cost cap per candidate |
| `structured_output_schema` | JSON Schema (Draft 7, ≤64KB) — makes the pass/fail verdict machine-readable instead of prose |
| `tags` | tag by case + candidate so the fan-out board can group them |
| `title` | readable session name for the demo |
| `session_secrets` | per-session secrets not stored org-wide |
| `idempotent` | avoid duplicate sessions on retry |

Response: `session_id`, `url`, `is_new_session`.

## Credentials handling

Config lives in `.env`, which is gitignored. Copy the template:

```bash
cp .env.example .env
# then paste the real values into .env — never into .env.example
```

Rules:
- The real key goes in `.env` only. Never in a committed file, a prompt, or a session log.
- `.env.example` holds placeholders and stays committed.
- If a key does leak, rotate it in *Settings → Service users* rather than editing history.
- Prefer Devin's own **secrets** feature (`secret_ids` / `session_secrets`) for anything
  the session itself needs, rather than pasting values into the prompt.

## Verify once the key arrives

```bash
set -a && source .env && set +a
curl -s -H "Authorization: Bearer $DEVIN_API_KEY" \
  "https://api.devin.ai/v3/organizations/$DEVIN_ORG_ID/sessions" | head -c 400
```

A 200 with a session list means we're wired up. A 401 means the key is wrong or the
service user lacks the RBAC role for that endpoint.

Docs: <https://docs.devin.ai/api-reference/authentication> ·
<https://docs.devin.ai/api-reference/v3/usage-examples>
