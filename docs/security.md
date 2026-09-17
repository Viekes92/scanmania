# Security model

ScanMania runs on an isolated LAN inside a shipping container. The threat model is *not* a hostile
internet — it is a guest who finds the venue Wi-Fi and starts poking at the NUC, plus the ordinary
need to keep player names and cloud credentials off an open endpoint.

## Admin authentication

The admin portal authenticates with a single shared password.

- The browser sends `X-Admin-Password: <sha256-hex of the password>`. It never sends plaintext.
- The server compares that hex digest against the digest of `SCANMANIA_ADMIN_PASSWORD` using
  `hmac.compare_digest`.
- Hashing the supplied value before comparison is what makes the non-ASCII case safe: Starlette
  decodes headers as latin-1, and `compare_digest` raises `TypeError` on non-ASCII `str`, which
  would surface as a 500 rather than a failed login.

**Auth fails closed.** With `SCANMANIA_ADMIN_PASSWORD` unset, every gated route returns 503 and no
credential is valid. There is deliberately no default password in the repo.

So an unset variable cannot lock the operator out of a live container, `__main__.py` generates a
random password at boot, exports it into the process environment, and logs it at WARNING:

```
journalctl -u scanmania -b | grep 'temporary admin password'
```

The unit is `scanmania` (there is no `scanmania-core`; the only two units are
`scanmania` and `scanmania-kiosk`), and `-b` limits it to the current boot —
the password is regenerated on every start, so an older one in the journal is
misleading rather than useful.

Make it permanent in **`/etc/default/scanmania`**, which is the file both units
read via `EnvironmentFile=`:

```
SCANMANIA_ADMIN_PASSWORD=...
```

then `systemctl restart scanmania`. (`/etc/scanmania/secrets.env` is read by
nothing — putting it there looks like it worked and silently does not.)

This is a shared password over plain HTTP on a trusted LAN. It is not a user system, and it is not
a substitute for network isolation.

## What is gated and what is not

Every mutation is gated. Every admin read is gated **except** three, which are consumed by
frontends that have no password:

| Endpoint | Consumer | Why it stays open |
|---|---|---|
| `GET /api/admin/runs` | GM console recent-runs list | GM iPad has no login |
| `GET /api/admin/beams` | GM console, `/admin/beams` overlay | same |
| `GET /api/admin/leaderboard` | outdoor display | public by design — it is on a screen outside |

These expose player nicknames, which are self-chosen at sign-in and already displayed publicly on
the outdoor screen. Do not add anything else to this list without a matching row here.

`/api/gm/*` is unauthenticated as a whole, including the hazer controls. The GM console is the
operator's tablet; adding a password to it would mean typing one in front of a queue.

## Download tokens

`window.location` and `EventSource` cannot send custom headers, so CSV exports, the DB snapshot,
and the journald log stream cannot use `X-Admin-Password`.

Instead the page calls `POST /api/admin/download-token` over the normal header-authenticated
channel and appends the returned token as a query parameter. Tokens are:

- random (`secrets.token_urlsafe(32)`),
- single-use — consumed on first request,
- short-lived — 60 seconds, with expired entries swept on each issue.

A token therefore leaks at most one download if it ends up in a proxy log or browser history.

## Input validation

- Journal unit names are checked against an allowlist (`_LOG_UNITS`); anything else is a 400.
  Nothing user-supplied reaches a shell — `journalctl` is invoked via
  `asyncio.create_subprocess_exec` with an argument list, never a shell string.
- Config file ids map through `_CONFIG_FILES` to fixed filenames; there is no path traversal
  surface because the client never supplies a path.
- Request bodies are Pydantic models with bounded lengths, value ranges, and name patterns.
- CSV exports prefix any field starting with `=`, `+`, `-`, `@`, tab, or CR with a quote, so a
  nickname cannot become a formula when the export is opened in Excel.

## Dev triggers

`POST /api/admin/dev/trigger` injects raw FSM events. It is available **only** when the process was
started with `--fake-all`, and returns 404 otherwise, so it cannot be used to fake a clean run on
the production box. `GET /api/admin/dev/available` tells the UI whether to render the controls.
