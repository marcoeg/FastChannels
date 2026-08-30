# TVShell remote control — Hermes integration runbook

Hermes drives the TVShell kiosk viewer (the TV on 192.168.50.71) through
TVShell's remote-control MCP server, running locally on nucbox as a bridge.
Built 2026-08-29. Skill: `~/.hermes/skills/tv-shell-remote/SKILL.md`
(installed VERBATIM from `~/Development/TVShell/apps/mcp/REMOTE-SKILL.md` —
it is TVShell-owned content; re-copy on TVShell updates, don't fork).

## Architecture

```
Hermes ──http──► tvshell-mcp.service (nucbox, 127.0.0.1:8790/mcp, NO auth → loopback only)
                     │ ws (auto-reconnect, hello+token)
                     ▼
            TVShell proxy  ws://192.168.50.71:8787/api/remote/control
                     │
                     ▼
            kiosk shell (the TV) — 16 tools: tune/surf/guide/lens/volume/state
```

## Moving parts (nucbox)

| Piece | Where | Control |
| --- | --- | --- |
| MCP bridge | `~/Development/TVShell/apps/mcp/dist/index.js` | `systemctl --user <status\|restart> tvshell-mcp` |
| Its env (incl. the remote token) | `~/.config/tvshell-mcp.env` (600) | edit + restart |
| Hermes registration | `mcp_servers.tv_shell` in `~/.hermes/config.yaml` | config live-reloads |
| Rebuild after TVShell updates | `cd ~/Development/TVShell && pnpm install && pnpm -r build`, then restart the service; re-copy the skill file | |

Env contract: `TV_SHELL_PROXY` (http base — ws URL is derived),
`TV_SHELL_ACTOR=hermes` (NEVER reuse this actor name elsewhere — corpus
telemetry separates agent tunes from real viewing by it),
`TV_SHELL_REMOTE_TOKEN` (must match the proxy's `shell.remote.token`),
`TV_SHELL_MCP_TRANSPORT=http`, `TV_SHELL_MCP_HOST=127.0.0.1` (the /mcp
endpoint has no auth by design — keep it loopback), `TV_SHELL_MCP_PORT=8790`.

## Required config on 192.168.50.71 (the TVShell machine)

In `~/Development/TVShell/apps/proxy/tv-shell.config.json` (gitignored,
lives only there):

```jsonc
"proxy": { "host": "0.0.0.0", "port": 8787 /* … */ },
"shell": { "remote": { "enabled": true,
                        "token": "<same value as TV_SHELL_REMOTE_TOKEN>",
                        "state_push": true } }
```

then restart the proxy. Without `host: 0.0.0.0` the proxy binds loopback
and nucbox cannot reach it (the schema default; verified closed from the
LAN before this change). The token matters: a LAN-exposed proxy without
one lets anyone on the network drive the TV.

## Verification / smoke tests

```bash
# zero-dep protocol check (bypasses MCP entirely):
node ~/Development/TVShell/scripts/remote-demo.mjs \
  --url ws://192.168.50.71:8787 --actor hermes-smoke \
  --token "$(grep TV_SHELL_REMOTE_TOKEN ~/.config/tvshell-mcp.env | cut -d= -f2)" state

hermes mcp list          # tv_shell … all ✓ enabled
hermes mcp test tv_shell
hermes -z "what is playing on the TV right now?"   # get_state or honest no_shell
```

## Gotchas

- `no_shell` = the kiosk browser is not connected to the proxy — report,
  don't loop. `not_ready` = shell booting, one retry.
- **Skill routing lives in 57 characters.** Hermes' always-on skill index
  truncates every frontmatter description to 60 chars
  (`SKILL_PROMPT_DESC_LIMIT`, `agent/skill_utils.py`) — routing prose past
  that point is INVISIBLE to the model. All three TV skills' descriptions
  are written for that budget (this one front-loads "See or change what
  the TV is playing NOW"; epg-graph/fastchannels cede "the TV" entirely).
  Verified 2026-08-29: with the long descriptions, "what's playing on the
  TV" misrouted to guide listings; after the rewrite, TV-state and
  schedule questions route correctly.
- **The bridge's reconnect loop can stall** if it starts while the proxy
  is unreachable (a connect attempt that neither errors nor closes leaves
  no retry scheduled — despite the 15 s backoff cap). Symptom: tools say
  "no viewer state available" while `remote-demo.mjs` sees real state,
  and `ss -tn | grep 8787` shows no connection. Fix:
  `systemctl --user restart tvshell-mcp`.
- **Gateway sessions need a restart to pick up new MCP servers**: config
  live-reload covers `hermes -z` one-shots, but the long-running gateway
  (interactive chat + voice) kept its old toolset until
  `systemctl --user restart hermes-gateway`.
- Channel identity: `channel_id` here == FastChannels `tvg-id` == the EPG
  graph's ledger channel id — one namespace across all three systems.
  TVShell channel NUMBERS are proxy-pinned presentation (a third numbering,
  distinct from both FastChannels db ids and tvg-chno) — always resolve by
  name via `find_channel`.
- The bridge caches the lineup 10 minutes — newly added channels lag in
  `find_channel`.
- `toggle_captions` is a true toggle: never blind-retry it.
- The bridge reconnects forever with backoff; proxy/viewer restarts are
  absorbed. In-flight commands time out at 10 s.
- Every Hermes command lands in TVShell's telemetry as `remote_command`
  with `actor: hermes` (`scripts/inspect.sh` on the TVShell machine shows
  them) — by design, so agent-driven tuning never pollutes the
  viewing-preference signal.

## Future (documented, not built)

The proxy pushes viewer-state changes (`state_push: true`) to all
controllers; the MCP layer is deliberately pull-only. A v2 could subscribe
Hermes to those pushes for reactive context ("user just tuned X") — that
would be a custom WS integration, not this MCP server.
