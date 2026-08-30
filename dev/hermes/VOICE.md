# Hermes Voice — LiveKit bridge runbook

Talk to Hermes by voice from a browser. Built 2026-08-29; design in the
approved plan (v1 = SSE bridge; v2 = native LiveKit platform adapter using
Hermes' built-in `streaming_tts` seam, `gateway/platforms/base.py:4502`).

## Architecture

```
Browser mic ──wss:8443 (caddy TLS)──► livekit-server (host net, 7880/7881/7882udp)
                                          │ room "voice"
                                          ▼
                    hermes-voice-agent.service (dev/voice/agent.py)
                    silero VAD → OpenAI STT → HermesLLM → OpenAI TTS
                                          │ SSE
                                          ▼
                    Hermes gateway api_server platform  127.0.0.1:8642
                    session "voice-main", full agent (skills, MCP, memory)
```

## Using it

1. **Once per browsing device**: trust the mkcert root CA so the mic works
   over HTTPS. A plain-HTTP helper serves the cert for first-time setup:
   `http://nucbox.local:8880/hermes-ca.cer` (DER; also at the IP).

   **iPhone (the fussy one):**
   1. On the same Wi-Fi, open Safari →
      `http://nucbox.local:8880/hermes-ca.cer` → Safari asks to download a
      configuration profile → **Allow**.
   2. Settings → **Profile Downloaded** (or General → VPN & Device
      Management) → the mkcert profile → **Install**, enter passcode,
      Install again.
   3. The step everyone misses: Settings → **General → About →
      Certificate Trust Settings** → toggle **ON** full trust for the
      mkcert root. Safari refuses the site without this.
   4. Safari → `https://nucbox.local:8443` → Connect → allow the mic.
      (If `nucbox.local` doesn't resolve, use `https://192.168.50.17:8443`
      — the cert covers the IP too.)

   **Android (Chrome):** Android usually cannot resolve `.local` names —
   use the IP URLs throughout.
   1. Chrome → `http://192.168.50.17:8880` — any path on that port
      triggers the certificate download; the file lands in Downloads (no
      installer prompt; that's normal).
   2. Settings → search **"certificate"** → **Install a certificate** →
      **CA certificate** (exact path varies by vendor; stock Android:
      Security & privacy → More security → Encryption & credentials) →
      "Install anyway" past the warning → pick `hermes-ca.cer` from
      Downloads.
   3. No trust toggle needed — Chrome trusts user-installed CAs directly.
      Android will show a persistent "Network may be monitored"
      notification while a user CA is installed; informational only.
   4. Chrome → `https://192.168.50.17:8443` → Connect → allow the mic.

   **macOS**: download the same URL, open in Keychain Access, set the
   mkcert root to Always Trust. **Chrome/Linux**: Settings → Privacy →
   Security → Manage certificates → Authorities → Import
   `rootCA.pem`.
2. Open **https://nucbox.local:8443** (or `https://192.168.50.17:8443`).
3. Click **Connect**, allow the microphone, and just talk. The orb glows
   green while you speak, blue while Hermes speaks. Barge in any time —
   interrupting cancels Hermes' turn server-side.

The conversation lives in Hermes session **`voice-main`** and persists
across visits; Graphiti memory hooks fire on every turn. To start a fresh
transcript: `curl -X DELETE .../api/sessions/voice-main` + recreate (below).

## Moving parts

| Piece | Where | Control |
| --- | --- | --- |
| livekit-server + caddy | `dev/voice/compose.livekit.yml`, project `hermes-voice` | `sg docker -c "docker compose -f compose.livekit.yml -p hermes-voice <up -d\|down>"` (from `dev/voice/`) |
| Voice worker | `dev/voice/agent.py` + `hermes_bridge.py` | `systemctl --user <status\|restart\|stop> hermes-voice-agent` |
| Hermes API door | api_server platform, `127.0.0.1:8642`, enabled by `API_SERVER_KEY` in `~/.hermes/.env` | remove the key + `systemctl --user restart hermes-gateway` to disable |
| Model alias | `gateway.api_server.model_routes` in `~/.hermes/config.yaml` maps virtual `hermes-agent` → `gpt-5.6-terra`/`openai-codex` (without it, runs fail with "model not supported") | |
| Browser token | `dev/voice/mint_token.py [days]` → regenerates `client/config.js` (30-day default — re-run when expired) | |
| TLS cert | `dev/voice/ca/` (mkcert; cert valid to 2028-11) | |

Secrets (`dev/voice/.env`, `livekit.yaml`, `ca/`, `client/config.js`) are
git-excluded via `.git/info/exclude`.

## Voice style

Session `voice-main` was created with `model: gpt-5.6-terra` and its first
message instructs: spoken answers, concise, no markdown, Pacific times. To
recreate after deleting the session:

```bash
KEY=$(grep '^API_SERVER_KEY=' ~/.hermes/.env | cut -d= -f2)
curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"id":"voice-main","title":"Voice","model":"gpt-5.6-terra"}' \
  http://127.0.0.1:8642/api/sessions
# then send the style message via .../api/sessions/voice-main/chat
```

The bridge additionally strips residual markdown per-delta and speaks a
filler ("One moment, let me check.") when a tool starts before any text —
EPG/tool turns take 5–15 s.

## Driving the TV by voice

Voice sessions run on the `api_server` platform, which inherits all enabled
MCP servers — including `tv_shell` (the TVShell viewer remote). So "put on
the news", "tune to CNN", "mute the TV", "what's on the screen?" work by
voice end-to-end: LiveKit → Hermes → `mcp__tv_shell__*` → the kiosk.
See `dev/hermes/TVSHELL.md`.

## Logging the conversation

Three layers, two automatic:

1. **Voice transcript (worker-side)** — `~/news/voice-conversations.jsonl`,
   one JSON line per finalized turn: `{ts, role, text, interrupted, room}`.
   This is what STT actually heard and what TTS actually spoke, including
   barge-ins. Tail it live while talking:
   `tail -f ~/news/voice-conversations.jsonl | jq -r '"\(.role): \(.text)"'`
2. **Agent transcript (Hermes-side)** — the authoritative record: session
   `voice-main` in Hermes' state DB, including tool calls the voice layer
   never sees. View via the API:
   `KEY=$(grep '^API_SERVER_KEY=' ~/.hermes/.env | cut -d= -f2); curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8642/api/sessions/voice-main | jq`
   or open the session in the Hermes dashboard.
3. **Long-term memory** — Graphiti hooks fire on every voice turn, so the
   conversation feeds the `hermes` memory graph like any chat.

Audio recording (the actual sound) is NOT captured; that would need
LiveKit's egress service — deliberately out of scope.

## Troubleshooting

- **No mic prompt / "connect failed"**: the CA isn't trusted on that device,
  or you're on plain HTTP. Mic requires the HTTPS origin.
- **Connected but silent**: check `systemctl --user status hermes-voice-agent`
  and its journal for `registered worker`; check Hermes side with
  `curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8642/v1/capabilities`.
- **"model is not supported" in answers**: the `model_routes` alias is
  missing (see table) or the session was created without a model.
- **Token expired**: `./mint_token.py 30` and reload the page.
- **Two people talking**: one Hermes session — turns serialize; it's a
  single-user bridge by design.
- Latency: non-tool turns ≈ 2–3 s speech-end → first audio (OpenAI STT+TTS);
  tool turns are covered by the filler. For faster voices later: swap
  `openai.TTS()` in `agent.py` (ElevenLabs key exists) — or build v2, the
  native adapter with first-clause streaming (`streaming_tts` seam +
  `gateway/streaming_tts_consumer.py`).
