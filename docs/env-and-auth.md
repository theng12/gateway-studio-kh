# `.env` is read live, and the key is checked per request

Moved from `AGENTS.md` on 2026-11-08 (owner: "Trim everything"). `AGENTS.md` keeps the rules; this file keeps the incidents and measurements behind them.

`.env`, the documented no-UI key source, was not read live: `config.get()`
snapshotted at import, `providers/credentials.py` held a **second** copy
refreshed by `reload_env()`, and `api.require_auth` compared an import-time
`API_KEY` constant — fixing `GATEWAY_API_KEY`. **A rotation is the failure
that matters**: change the key, restart nothing, the old key still opens the
door.

`get()` re-stats `ENV_PATH` and re-reads on change; the credential store reads
through it, no `reload_env()` to forget; `require_auth` reads
`gateway_api_key()` per request. `admin.require_admin` already did. In
`tests/test_e2e.py` the suite key lives in the temp `.env`, not the spawned
environment, because `get()` prefers `os.environ` and a key passed there would
shadow the file and make the rotation untestable. It asserts a *wrong* key is
refused first: a failed `.env` leaves a keyless gateway open.

**`/admin/api/*` is deliberately open from loopback** (`require_admin` trusts
loopback and demands the token only when bound elsewhere), which is why a `curl`
against `/admin/api/status` returns 200 with no header. Measure auth on the
Story surface (`/v1/...`), which needs the key.
