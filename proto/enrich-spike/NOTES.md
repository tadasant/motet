# Agentic enrichment spike — running notes

Branch `proto/local-ux`, dir `proto/enrich-spike/`. Everything here is throwaway.

## Toolchain (verified on npm, 2026-09-12)

| Package | Version | Role |
|---|---|---|
| `@mariozechner/pi-coding-agent` | 0.73.1 | the `pi` CLI (bin `pi`). Repo moved to `earendil-works/pi`; npm name unchanged. |
| `pi-mcp-adapter` | 2.33.0 | Pi extension that attaches MCP servers (nicobailon/pi-mcp-adapter) |
| `playwright-stealth-mcp-server` | 0.2.3 | one `browser_execute(code)` tool, `page` in scope; same server as `.mcp.json` |

Installed locally with `npm i` in this directory (gitignored `node_modules/`), run via `npx pi`.

## Pi: how it runs

- **Non-interactive:** `pi -p --mode json "<prompt>"` prints every session event as JSON lines
  (`session`, `agent_start`, `turn_start`, `message_*`, `tool_execution_start/end`, `turn_end`,
  `agent_end`). `message_end` carries `usage` incl. **cost in USD** (from OpenRouter's usage
  accounting). **Pitfall:** in print mode Pi also reads stdin and merges it into the prompt, so
  under a tool harness with an open stdin pipe it hangs forever — always `</dev/null`.
- **Provider/key:** `--provider openrouter` reads `OPENROUTER_API_KEY` from env. No Anthropic
  key is needed. Pi's built-in OpenRouter catalogue stops at `anthropic/claude-sonnet-4.6`;
  `anthropic/claude-sonnet-5` (the project default) is added through a `models.json` in the
  agent dir (see below) and works.
- **Isolation:** `PI_CODING_AGENT_DIR=<dir>` moves Pi's home (`settings.json`, `models.json`,
  `sessions/`, installed extensions). The machine's `~/.pi/agent/settings.json` points at
  three extensions in unrelated clones, so the spike uses `.pi-home/` here (gitignored).
- **Session transcript on disk:** `~/.pi/agent/sessions/<cwd-slug>/<timestamp>_<uuid>.jsonl`
  (tree-structured JSONL with `id`/`parentId`). `--session <path>` pins the file.
- **System prompt:** `--system-prompt <text|file>` replaces, `--append-system-prompt` appends.
  `--no-context-files` stops it reading this repo's `AGENTS.md` (which is large and irrelevant).
- **Tools:** `--tools <csv>` allowlists; `--no-builtin-tools` keeps only extension tools.
  Extensions: `-e <path|npm:…>` or `pi install npm:pi-mcp-adapter` (writes settings.json).

## pi-mcp-adapter: config and OAuth

- Config discovery (later wins): `~/.config/mcp/mcp.json` → `~/.agents/mcp.json` →
  `<agent dir>/mcp.json` → `./.mcp.json` → `./.pi/mcp.json`; or `--mcp-config <file>`.
  **The repo's `.mcp.json` shape is accepted as-is** (Claude Code / Cursor compatible).
- Stdio server: `{command, args, env, inheritEnv, lifecycle}`. Remote: `{url, auth: "oauth"|"bearer",
  headers, oauth:{…}}`. `${VAR}` interpolation works in `url`/`headers`/`env`.
- By default all MCP tools sit behind one `mcp` proxy tool (`mcp({search})`, `mcp({tool,args})`);
  `directTools: true` registers them as first-class tools named `<server>_<tool>`.
- **OAuth for a remote server:** the SDK's OAuth 2.1 + PKCE; discovers
  `/.well-known/oauth-protected-resource`, uses **Dynamic Client Registration** when no
  `clientId` is configured, starts a loopback callback server on an OS-assigned port, opens the
  browser, exchanges the code, and refreshes automatically afterwards.
- **Token storage:** the OS credential store via `@napi-rs/keyring` — on macOS the **login
  Keychain**, service `pi-mcp-adapter.oauth`, account = the server name from the config
  (e.g. `email`), URL-bound so it is refused for a different URL. Never a plaintext file
  (`oauthDir`/`MCP_OAUTH_DIR` are legacy *import* locations only). Fails closed if no store.
- Headless/non-interactive: `mcp({action:"auth-start", server})` returns the auth URL and arms
  the callback; `mcp({action:"auth-complete", server, args:{redirectUrl|code}})` finishes it.
  `settings.autoAuth: true` triggers the flow from a tool call. Interactive: `/mcp-auth <server>`.
- **Approval:** `approveTools` gates fail closed as `approval_required` in headless sessions;
  leave it unset for an autonomous run.

## The email MCP server (strad)

Probed with curl (host redacted here; it is `$PROTO_EMAIL_MCP_URL`):
- `POST /mcp` unauthenticated → `401`, `WWW-Authenticate: Bearer realm="strad", resource_metadata=…/.well-known/oauth-protected-resource/mcp`.
- Protected-resource metadata: `authorization_servers: [<same host>]`, `scopes_supported: ["mcp"]`.
- AS metadata: `authorization_endpoint /oauth/authorize`, `token_endpoint /oauth/token`,
  **`registration_endpoint /oauth/register` (open DCR)**, grants `authorization_code` +
  `refresh_token` only (no `client_credentials`), PKCE S256, `token_endpoint_auth_methods: none`.
- Its docs say: `/mcp` accepts **either** an OAuth access token (hourly expiry, **refresh tokens
  never expire**, the human behind it is authenticated with Google) **or** a long-lived
  **static system token** (`strad_<env>_…`) minted out of band in its `/tokens` console —
  that is the credential meant for agents.

Consequence: whichever door, **one human step is unavoidable** (invariant 9): either click
through Google consent once (OAuth; token lands in the Keychain and refreshes forever), or mint
a static token once and hand it to the adapter as `auth: "bearer"` + `bearerTokenEnv`.
`.env` carries only the URL, so this spike takes the OAuth door.

## The article

Dev DB `source_items` holds TheInformation newsletters (e.g. `si_f9f058e18424`, "Confusion
Reigns Over White House's AI Whitelist", 2 paragraphs + "Read the full article: <SendGrid
click-tracking link>"). The tracking link 403s for curl (any UA); a real Chromium resolves it to
`https://www.theinformation.com/articles/confusion-reigns-white-houses-ai-whitelist?eu=<per-recipient token>`
— and lands on a **Cloudflare challenge** ("Just a moment…", `__cf_chl_rt_tk`) in plain headless
Chromium. `resolve-link.mjs` is the resolver.

## Storage state

`playwright-stealth-mcp-server` exposes only `page` to `browser_execute`, but `page.context()`
is the BrowserContext, so `await page.context().storageState()` exports cookies+localStorage
and `page.context().addCookies(state.cookies)` re-imports them — no server change needed. The
server has **no** env var to seed a context from a file at launch; it only uses `storageState`
internally when it recycles the context for video recording.

## What actually happened (chronological)

1. **Package rename trap.** `@mariozechner/pi-coding-agent` is stale on npm (0.73.1, last
   published 2026-05). `pi-mcp-adapter` 2.33.0 imports `@earendil-works/pi-coding-agent` and
   fails to load under the old package. The current Pi is **`@earendil-works/pi-coding-agent`
   0.85.1**. Installed that; adapter loads.
2. Pi + OpenRouter + Sonnet 5: `PONG` smoke test, $0.0085, 3.7 s. Cost reported per message.
3. `.pi/mcp.json` (project override, cwd = this dir) with `playwright` (stdio, stealth,
   headless, `directTools` narrowed to 4 of the server's 6 tools) and `email` (remote).
4. **OAuth against strad via the adapter: started, never completed.** `auth-start` did DCR
   (a `client_id` came back), PKCE, loopback callback on an OS port, and `open`ed the system
   browser on `/oauth/authorize`, which 302s to **`accounts.google.com` with `hd=<owner domain>`,
   `prompt=consent`** — a Google sign-in as the owner. No human was at the machine; the
   adapter's callback wait is hard-capped at **5 minutes** (`CALLBACK_TIMEOUT_MS`), after which
   `authenticate()` rejects with "Authorization cancelled". Tried twice (model-driven via
   `auth-email.sh`; model-free via `auth-email.mjs` run with `tsx`, because Node refuses to
   type-strip files under `node_modules`). Had it completed, the token would live in the macOS
   login Keychain under service `pi-mcp-adapter.oauth`, account `email`. Kept both scripts as
   the documented alternative.
5. **The owner's answer: the credential already exists inside Motet.** Connector
   `cn_e1bd093da927` (kind `mcp`, the strad URL, `status ready`, `has_secret`) holds the
   access/refresh token set sealed with the vault. `mcp_token.py` opens it exactly as a worker
   would (`build_key_manager()` → kms with `GOOGLE_APPLICATION_CREDENTIALS`, then
   `load_connector_secret`), refreshes through the row's `oauth_token_endpoint` when within
   60 s of expiry (or `--refresh`), re-seals the new set with `store_connector_secret`, and
   prints the access token alone. `email-bearer.sh` wraps it as `Bearer …`, and the adapter
   config reads it at connect time through the **`!command` header hook** — the token is
   never in a file, an env var, or the transcript. Proved: `initialize` → 200; adapter lists
   5 `gmail-ro` tools; `--refresh` rotated the token and the new one authenticates.
6. **URL gotcha:** `PROTO_EMAIL_MCP_URL` already carries `?servers=gmail-ro`; appending it
   again yields slug `gmail-ro?servers=gmail-ro` → `downstream_not_authenticated`.
7. **End-to-end run succeeded** (`runs/20260912-184308`): 11 tool calls, 160 s, $0.41,
   1,106-word article. Sequence: load article (walled) → find `/sessions/new` → fill
   `#login-email`, submit "Continue with email" → site says "Check your inbox … click the
   link … expires in 2 hours" (**a magic link, not a code**) →
   `email_gmail-ro__search_email_conversations {query:"from:theinformation.com newer_than:1d"}`
   → `get_email_conversation` on the newest "Sign in to The Information" (arrived 2 s after
   submit) → `page.goto(<magic link>)` → redirected to the full article, logged in → extract
   → `page.context().storageState()`.
8. **Second run with `--state`** (`runs/20260912-184612`), different article: 4 tool calls,
   `LOGGED_IN: not-needed`, 626-word article, $0.35 / 237 s — most of both being the model
   echoing 10 KB of storage-state JSON through its output. The harness, not the model,
   should do that export.
9. **`eu=` finding.** TheInformation's newsletter links resolve (through SendGrid click
   tracking, which 403s curl) to `…/articles/<slug>?eu=<per-recipient token>`. A stealth
   headless load of that URL returned the **full article (18 paragraphs) with no login at
   all** in a direct probe (`eu-probe.mjs`) — but the agent run on the raw tracking link
   (`runs/20260912-183917`) still hit the wall. Not resolved; likely the token is consumed
   on the SendGrid hop or is single-use. Worth a look before building the login dance for
   this publisher, since the ingested email already carries that link.
10. Cloudflare: plain headless Chromium gets the "Just a moment…" challenge on
    theinformation.com; the stealth server (`STEALTH_MODE=true`) did not, in any run.

## Redaction

`runs/*/transcript.jsonl` and `pi-session.jsonl` contain the mailbox search results, the
login email's body (with the magic link, 2-hour expiry) and the exported cookies. `runs/` is
gitignored; `summarize.py` redacts links, addresses and 6-digit codes for quoting.
