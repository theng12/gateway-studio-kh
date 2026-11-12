# GatewayKH agent connector (MCP)

An **MCP server** (Model Context Protocol) that lets an AI agent — Claude Code, Claude
Desktop, any client that speaks MCP — make images, speech, music, videos, transcripts and
chat replies **through your Gateway**, using a key you issued to a guest.

The agent is a guest, not an admin. It can make things; it cannot change the Gateway.

---

## What you do once: create a guest key

1. Open the Gateway console at `/admin` (the app window, or the same page in a browser over
   Tailscale).
2. Go to **Guests**. Add a guest if there is not one already (a name and a password — the
   password is only for the browser; the agent uses the key).
3. In that guest's **Agent key** column, press **Create key**.
4. **The key is shown once.** Copy it now; it is stored only as a hash and cannot be shown
   again. It starts with `gwk_`.
5. Put the key in the `env` block of the MCP config below (`GATEWAY_GUEST_KEY`). Secrets
   live in the app's own data directory, mode 0600 — never in a system credential store.

**Replace key** makes a new one and retires the old immediately. **Revoke key** stops the
agent on its next request; the guest's account and its jobs stay. Disabling or removing the
guest stops the key too. Paid models stay **off** for a new guest until you tick **paid
models** on that guest.

---

## Ready-to-paste MCP config

Both clients take the same block. Replace the two values: `GATEWAY_URL` is your Gateway's
**Tailscale** address (the connector never guesses one), and the `args` path is this
repository's `connectors/mcp/server.py` on the Mac running the client — or, on a Mac with
the Gateway app installed, the copy that ships inside it:
`/Applications/gateway-kh.app/Contents/Resources/gateway/connectors/mcp/server.py`.

```json
{
  "mcpServers": {
    "gateway": {
      "command": "python3",
      "args": ["/Users/YOUR-NAME/Developer/gateway-kh/repository/connectors/mcp/server.py"],
      "env": {
        "GATEWAY_URL": "http://100.x.y.z:8787",
        "GATEWAY_GUEST_KEY": "gwk_your_key_here",
        "GATEWAY_OUTPUT_DIR": "/Users/YOUR-NAME/GatewayAssets"
      }
    }
  }
}
```

- **Claude Code** — put the block in a project's `.mcp.json`, or run:

  ```sh
  claude mcp add gateway --env GATEWAY_URL=http://100.x.y.z:8787 \
    --env GATEWAY_GUEST_KEY=gwk_your_key_here \
    -- python3 /Users/YOUR-NAME/Developer/gateway-kh/repository/connectors/mcp/server.py
  ```

- **Claude Desktop** — add the block to
  `~/Library/Application Support/Claude/claude_desktop_config.json`, then quit and reopen
  Claude.

The connector needs only `python3` — nothing to `pip install`.

---

## What the agent can do

| Tool | What it does |
| --- | --- |
| `list_models` | The models **this key** may use, per modality, with their published limits and options |
| `list_voices` | Voices usable for speech |
| `generate_image` | Text-to-image |
| `generate_speech` | Text-to-speech, into a chosen voice |
| `generate_video` | **Image-to-video** from a scene image on the agent's Mac |
| `transcribe` | Upload audio, get a transcript |
| `generate_music` | Music generation |
| `chat` | One chat reply |
| `get_job` | A job's honest state and its outputs |
| `wait_for_job` | Poll for a bounded time; says "still running" instead of pretending |
| `download_result` | Save a finished output into `GATEWAY_OUTPUT_DIR` |

**Jobs are asynchronous.** Every `generate_*` tool answers immediately with a job id and a
state (`queued`, `running`, …). Nothing is done until `get_job` says `succeeded`. The model
lists and their options come from the Gateway's live catalogue on every `tools/list` — there
is no model list written down in the connector.

## What it cannot do

- **No admin, no fleet, no settings.** Every owner page and route refuses a guest key, and
  **every `/v1` route refuses it too** (`/v1` is the owner's API: fleet, account, content,
  final renders). The key opens the Create API and nothing else.
- **No paid models** unless you ticked **paid models** for that guest. A paid model is
  refused when submitted, not just hidden from the list.
- **No other guest's work.** A job or file that is not this guest's answers exactly like one
  that never existed.
- **No text-to-video.** Image-to-video only; `image_path` is a required field.
- **No writes outside `GATEWAY_OUTPUT_DIR`.** `download_result` refuses `..`, absolute
  paths and anything that resolves outside that folder.
- **The key is never printed.** It travels in the `Authorization` header, never in a URL,
  never into a file the connector writes, and tool output is redacted before it is sent.

## It stays on Tailscale

`GATEWAY_URL` should be the Gateway's **Tailscale** address (or `http://127.0.0.1:8787` on
the Gateway's own Mac). Do not port-forward the Gateway or put it on a public address: the
key is the second lock, not the first.

## When something is wrong

| What the agent says | What it means |
| --- | --- |
| `That guest API key is not valid … (GUEST_KEY_INVALID)` | Revoked, or the guest disabled/removed. Issue a new key. |
| `No guest key is configured` | Set `GATEWAY_GUEST_KEY` in the client's env block. |
| `The Gateway at … did not answer` | Wrong `GATEWAY_URL`, Gateway not running, or not on the same tailnet. |
| `… is a paid model. This guest account may use free and local models only` | The owner's switch for this guest, on the Guests page. |
| `is outside the allowed output folder` | `folder` must be a subfolder of `GATEWAY_OUTPUT_DIR`. |

## Tests

- `python3 -m unittest tests.test_agent_connector` — this server, driven over stdio against
  a fixture Gateway on an ephemeral port: every tool, async waiting, a refusal in the
  Gateway's own words, a dead key, and the key never appearing in a result.
- `python3 -m unittest tests.test_guest_api_keys` — the key path in the real Gateway: scope
  walked over every `/admin` and `/v1` route, paid refusal, isolation between two guests,
  revocation on the next request.
