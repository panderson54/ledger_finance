# Shared deployment: Ledger + Homebase behind one Google sign-in

One server, one domain, one sign-in with a Google account, and a strict allowlist
of pre-approved accounts. Each app's data stays physically separate.

```
                        https://ledger.example.com      https://home.example.com
                                     │                              │
                                     ▼                              ▼
  ┌───────────────────────────── Caddy (TLS, :443) ────────────────────────────┐
  │ 1. strips any client-supplied X-Forwarded-Email / X-Proxy-Secret           │
  │ 2. forward_auth → oauth2-proxy /oauth2/auth ── 401 → redirect to Google    │
  │ 3. on 202: copies the verified email into X-Forwarded-Email                │
  │ 4. adds that app's own X-Proxy-Secret and proxies to the app               │
  └───────┬──────────────────────────────┬──────────────────────────┬──────────┘
          │ edge network                 │ ledger_net               │ homebase_net
          ▼                              ▼                          ▼
   oauth2-proxy                     ledger:5001                 homebase:5100
   (Google OIDC,                    AUTH_MODE=proxy             AUTH_MODE=proxy
    allowed_emails.txt,             checks secret +             checks secret +
    cookie on .example.com)         LEDGER_ALLOWED_EMAILS       email ∈ users table
                                         │                          │
                                    ../data/finance.db       homebase/data/homebase.db
                                    (mounted ONLY here)      + uploads (mounted ONLY here)
```

## Design decisions

**Subdomains, not path prefixes.** Use `ledger.example.com` and `home.example.com`,
not `example.com/ledger` and `example.com/home`. The main reason is data
separation: two apps on different subdomains are different browser *origins*, so
JavaScript on one (including an XSS bug) can't read the other's pages or call its
API. Each app's session cookie is also host-only, so it's never sent to the other.
Under path prefixes the two apps would share one origin, and so would share
cookies and script access. A second reason: both apps hard-code root-relative
URLs (`fetch('/api/...')`, `href="/..."`, about 80 of them), and a path prefix
would break every one.

**A gateway handles auth, not code in each app.** oauth2-proxy runs the Google
OAuth/OIDC flow once for every app, and Caddy enforces it before a request reaches
either app. Neither app contains OAuth code or stores Google tokens. Adding a
third app takes one site block in the `Caddyfile`.

**The apps also check, as a second layer.** Each app has an `AUTH_MODE=proxy` mode
that:
- rejects any request without that app's own `X-Proxy-Secret` (401), so a request
  that reaches the container without passing through Caddy is refused even with a
  forged email header. The secret is different per app, so Ledger's secret opens
  nothing in Homebase.
- enforces its **own** allowlist (403): Ledger uses `LEDGER_ALLOWED_EMAILS`, and
  Homebase uses its existing `users` table. The gateway allowlist is the *union*
  of everyone who may use *any* app; each app then narrows it. So a partner can
  use Homebase without ever being able to open Ledger.
- refuses to start in proxy mode without its secret (and, for Ledger, its
  allowlist), so a missing setting can't silently leave the app open.

## Three allowlist layers

| Layer | Where | What it answers |
|---|---|---|
| 0 (optional) | Google Cloud consent screen: *Testing* status plus test users | Can this Google account even start sign-in? |
| 1 | `oauth2-proxy/allowed_emails.txt` | Can this account get past the gateway at all? |
| 2 | `LEDGER_ALLOWED_EMAILS` / Homebase `users` table | Can this account use *this* app? |

oauth2-proxy re-checks layer 1 on every request and reloads the file when it
changes, so removing a line locks that person out immediately.

> **Pitfall:** never set `OAUTH2_PROXY_EMAIL_DOMAINS`. oauth2-proxy admits an email
> if it is in the file *or* matches a domain, so `email_domains=*` admits every
> Google account in the world.

## Data separation

| Concern | How it's separated |
|---|---|
| Databases | Separate SQLite files. Each container mounts **only its own** data dir, so Ledger's process can't open `homebase.db`, and vice versa. |
| Network | Each app is on its own Docker network, shared only with Caddy. The apps can't reach each other or oauth2-proxy. No app publishes a host port. |
| Browser | Separate origins (subdomains), host-only session cookies with distinct names (`ledger_session`, `homebase_session`), per-app CSRF (a POST aimed at one app from the other's origin is rejected). |
| Secrets | Separate `SECRET_KEY` and `AUTH_PROXY_SECRET` per app. Each app's AI/API keys stay in that app's own config/DB. |
| Authorization | Per-app allowlists (layer 2 above). Homebase keeps its household scoping behind that. |
| Logs & backups | Separate log dirs and separate backup jobs per data dir (below). |
| Shared | Only identity (the Google email) and the oauth2-proxy sign-in cookie on `.example.com`. Signing out signs you out of every app. |

If you ever move off SQLite, keep the same rule: one database *and one DB role*
per app, and never share a schema.

## Setup runbook

The repos are checked out side by side on the server:

```
/srv/apps/ledger_finance   # this repo; the stack lives in ./deploy
/srv/apps/homebase
```

1. **DNS.** Point `ledger.<domain>` and `home.<domain>` (A/AAAA records) at the
   server. Open ports 80 and 443. Caddy gets Let's Encrypt certificates
   automatically.

2. **Google OAuth client.** In Google Cloud Console → *APIs & Services*:
   - *OAuth consent screen*: User type **External**, scopes `openid`, `email`,
     `profile` only. Leaving it in **Testing** and adding each allowed account
     as a *test user* gives you layer 0 for free.
   - *Credentials* → *Create OAuth client ID* → **Web application**, with
     *Authorized redirect URIs*:
     - `https://ledger.<domain>/oauth2/callback`
     - `https://home.<domain>/oauth2/callback`

     Each app host runs its own callback. Adding an app means adding its URI here.

3. **Secrets.** `cd deploy && cp env.example .env`, then fill it in. Generate
   **every** secret separately (the commands are at the top of the file).
   `.env` is gitignored.

4. **Gateway allowlist.** `cp oauth2-proxy/allowed_emails.txt.example
   oauth2-proxy/allowed_emails.txt`: one address per line, lowercase, every
   person who may use any app. This file is gitignored.

5. **Per-app allowlists.**
   - Ledger: `LEDGER_ALLOWED_EMAILS=you@gmail.com` in `.env`.
   - Homebase: link each Google account to a user (no password needed):
     ```bash
     docker compose run --rm homebase flask create-user --email you@gmail.com --name You --no-password
     ```
     Users you already created with the same email keep working. Their
     password is simply no longer used.

6. **Existing data.** By default the stack mounts each repo's existing `data/`
   and `logs/` dirs (`../data`, `../../homebase/data`), so current databases are
   picked up as-is. Back them up first (`python scripts/backup_db.py` for
   Ledger; copy `homebase.db` for Homebase). Containers run as uid 1000, so the
   data dirs must be writable by that uid (`chown -R 1000:1000` and
   `chmod 700`).

7. **Start it.** `docker compose up -d --build`, then check `docker compose logs -f`.
   Stop the old standalone deployments first: the per-repo `docker-compose.yml`
   files and the Ledger systemd/nginx setup publish ports directly and bypass
   the gateway.

8. **Verify** (in a private window):
   - [ ] `https://ledger.<domain>` redirects to Google, and after sign-in lands on Ledger
   - [ ] `https://home.<domain>` opens **without** a second sign-in
   - [ ] A Google account not in `allowed_emails.txt` gets oauth2-proxy's 403 page
   - [ ] An account in `allowed_emails.txt` but not in `LEDGER_ALLOWED_EMAILS` gets 403 from Ledger
   - [ ] From another machine, `curl http://<server-ip>:5001` and `:5100` fail to connect
   - [ ] *Sign out* in Ledger (or *Log out* in Homebase) signs you out of both

## Operations

- **Add a person:** add them to `allowed_emails.txt` (and as a Google test user,
  if you use layer 0). Then grant per app: add them to `LEDGER_ALLOWED_EMAILS` and
  run `docker compose up -d ledger`, and/or run
  `flask create-user --no-password` for Homebase.
- **Remove a person:** delete their line from `allowed_emails.txt`. That blocks
  every app immediately. Then remove them from the per-app lists too.
- **Backups:** back up each data dir on its own schedule and to its own
  destination. Ledger's financial data deserves an encrypted target.
  - `deploy/../data/finance.db`
  - `homebase/data/homebase.db` and `homebase/data/uploads/`
- **Rotate secrets:** change a value in `.env` and run `docker compose up -d`.
  Rotating `OAUTH2_PROXY_COOKIE_SECRET` signs everyone out. Rotating an app's
  `*_PROXY_SECRET` updates Caddy and the app together.
- **Upgrade oauth2-proxy:** the image is pinned. Bump the tag deliberately
  after reading the release notes.
- **Homebase MCP server:** it runs locally over stdio against the data dir and
  is never exposed over HTTP, so the gateway doesn't apply to it. Keep it that way.

## Local development

Nothing changes. With `AUTH_MODE` unset, Ledger runs open as before (`none`),
and Homebase uses its password login (`password`).

## What this does not protect against

- A compromised Google account on the allowlist. Turn on 2-Step Verification
  for every allowed account.
- Anyone with shell or Docker access on the server. They can read both data
  dirs. Encrypt backups and restrict SSH.
- A compromised Caddy or oauth2-proxy container. It holds the gateway secrets.
  Keep both images updated.
