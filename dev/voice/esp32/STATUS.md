# Minny voice satellite — project status

_Last updated 2026-10-04: troubleshooting note, BUG-0001, `sdkconfig` and
`dependencies.lock` now tracked. (2026-09-24 revision: wake word, wake-gated
STT, STT recycle, capture watchdog.)_

## TL;DR

A Waveshare **ESP32-S3-AUDIO-Board** runs as a standalone voice terminal for
the Minny agent: it joins the self-hosted LiveKit room `speaker` over WiFi,
sleeps until it hears the on-device wake word **"Hey Wanda"**, then holds
multi-turn conversations (EPG lookups, "what's on," plot summaries, TV
control) and goes back to sleep after ~30 s of quiet. It is **stable
unattended**: soaked 7 h with 8 STT recycles clean (2026-09-14/15), and it
self-recovers from its one known device-side failure (capture pipeline death)
by rebooting.

**Working today**
- Standalone: powered from any 5V USB charger (no computer needed); WiFi
  creds + a 1-year LiveKit token are baked into flash. Needs the nucbox host
  (LiveKit server + `voice-agent` worker) up on the LAN. `loginctl` lingering
  is on, so the backend survives reboots unattended.
- **Wake word on the device** (WakeNet `wn9_heywanda_tts`). While asleep the
  board publishes digital silence (-140 dBFS) — TV and room audio never
  leave it — and the agent keeps STT **off**, so there is no cloud cost
  while idle. On wake: chime → mic live → STT enabled.
- Status on the 7-LED RGB ring: dim standby = asleep, bright green =
  awake/listening, amber = thinking, blue = speaking, red = connection failed.

**Two accepted limitations**
1. **Half-duplex** — you must let Minny finish before speaking; no barge-in
   (see "Restoring full duplex" below). The phone/browser (room `voice`)
   is unaffected and keeps full barge-in.
2. **No LCD** — the 1.47" touchscreen module was dead on arrival and was
   removed; the LED ring is the status display instead.

## What was done, and why

The build was a chain of real hardware/SDK bugs, each fixed from evidence.
Bring-up: commits `c4cdb17` → `2d0120b`; wake word and hardening:
`e9ea045` → `4e6203d` (backend `c5c4709`, `81a911e`), all on `origin`
(private mirror).

| Problem (symptom) | Root cause | Fix |
| --- | --- | --- |
| Silent speaker | 4-slot/32-bit capture reconfigured the shared full-duplex I2S bus to a geometry that mutes the ES8311 DAC | 2-slot/16-bit capture (`channel=2` then dev_src), `es8311 use_mclk=false` — the only geometry with confirmed playback |
| Network flooded, instability | SDK hardcoded a data channel on the **subscriber** peer; LiveKit runs no SCTP there, so it sent SCTP INIT ~10×/s forever | SDK patch: enable the data channel only on the publisher peer (`sdk-patches/0001-…patch`) |
| Hard hang after ~2 exchanges | **JTAG halt proved a deadlock** in the esp-sr AFE (acoustic front-end); tasks frozen on kernel locks, only the network ISR alive (still pinged) | Removed the AFE — swapped `esp_capture_new_audio_aec_src` → `esp_capture_new_audio_dev_src` (raw PCM). Not needed with half-duplex |
| Minny transcribed and answered herself (echo loop) | Removing the AFE removed the only echo suppression; the hot mic hears the speaker | Smaller playout buffer (16 KB, ~0.5 s) + a 1 s post-speech mic cooldown on the agent, both scoped to room `speaker` |
| "Stuck / no agent" after restarts | LiveKit never re-dispatches an agent into a room that already exists; the always-on device orphans its room on every worker restart | `voice-room-healer.timer` deletes agent-less rooms every 2 min → device rejoins → fresh agent |
| Phone lost barge-in | half-duplex config was global | Made `turn_handling` per-room: half-duplex for `speaker` only, full-duplex for `voice` |
| Wrong LED colors | Ring is wired **RGB**, firmware assumed GRB (red/green byte swap) | `LED_STRIP_COLOR_COMPONENT_FMT_RGB` |
| STT code-switching ("hi Minny" → Norwegian/Thai) | realtime STT auto-detect on short raw-mic phrases | Pin `language="en"` + topic prompt |
| Device transcribed the TV all day | always-on mic, no local gate | On-device WakeNet ("Hey Wanda") on the raw mic — standalone `esp_wn_iface`, NOT the AFE; 8 KB **internal-RAM** stack, pinned core (`e9ea045`) |
| Replies silent after waking | (a) gating by `esp_capture_stop/start` toggles the shared I2S record device and silences playback; (b) the wake chime called `esp_codec_dev_close()` on the play device, clearing av_render's `output_opened` flag | Capture runs **continuously** through a gated-tee source (`wake.c`) that publishes silence while asleep; the chime opens-if-needed and never closes (`c397f67`) |
| Encoder reboot-loop after adding the gated source | registering our esp_capture thread scheduler (to pin WakeNet's stack internal) **replaces** the SDK's, dropping its 40 KB Opus stack | `media.c` scheduler reproduces the 40 KB stack for `aenc_0` |
| Cloud STT billed 24/7 on silence; next request swallowed | device streams continuously, so STT ran on the idle silent stream and the Realtime session timed out | Gate on the **agent**: STT disabled while asleep, enabled on the device's `wake` data message (topic `session`), disabled on idle-sleep/disconnect (`c5c4709`) |
| Agent session died ~hourly, reconnect-thrashing the device | OpenAI caps a realtime STT session at 60 min; the plugin's own recycle is activity-gated, so the idle satellite ages out | Satellite-only ~50-min timer drives the plugin's own reconnect path; guarded to a logged no-op if a plugin upgrade renames the internals (`81a911e`) |
| Device "connected" but deaf overnight | reconnect churn desynced the shared-I2S enable state → AUD_SRC job failed → capture (and WakeNet with it) stopped | Watchdog: `wake_frame_ticks()` must advance while CONNECTED, else `esp_restart()` after 30 s; fresh boot is asleep again in ~15 s (`4e6203d`) |

**Debugging methods worth keeping** (in the tree):
- `main/net_log.c` — the device streams `esp_log` over UDP to
  `udp://<host>:5514`. Opening the USB console **resets** the board, which
  hid failures; the WiFi log is the non-invasive view.
- JTAG over the built-in USB: `openocd -f board/esp32s3-builtin.cfg`, then
  `xtensa-esp32s3-elf-gdb build/minny_satellite.elf` → `thread apply all bt`.
  Needs `sudo chmod 666 /dev/bus/usb/<bus>/<dev>` for the S3's USB-JTAG node
  (the node number changes on re-enumeration).
- The UDP log is **quiet while the board sleeps** — say the wake word to get
  a live trace. A reboot in the log is most likely the capture watchdog
  ("capture pipeline dead ... rebooting"), i.e. a self-recovery, not a crash.

## Running it — backend operations

Everything is systemd **user** units on nucbox (`systemctl --user …`,
`journalctl --user -u …`):

| Unit | Role |
| --- | --- |
| `voice-agent.service` | The worker. Restart after editing `voice_agent.py` or prompts. `Conflicts=hermes-voice-agent.service`. Its `ExecStartPost` runs `heal_agentless_rooms.py` twice (at +75 s and +135 s) to re-dispatch the satellite's orphaned room after a restart (log: `/tmp/voice-room-healer.log`) |
| `voice-room-healer.timer` | Deletes agent-less rooms every 2 min — LiveKit never re-dispatches into an existing room, so this is what heals the always-on device after any worker death |
| `voice-agent-restart.timer` | Daily 04:30 `try-restart` of the worker (a worker left idle for days ended up with a dead prewarmed process pool) |
| `voice-memory-backup.timer` | Weekly snapshot of the memory FalkorDB |

Satellite-only behaviour lives in `voice_agent.py`, keyed to room `speaker`:
half-duplex (interruption off + discard-while-speaking), 1 s post-speech mic
cooldown, wake-gated STT, the ~30 s idle `sleep` message, and the ~50-min STT
recycle. Room `voice` (phone/browser) keeps full barge-in, no wake word, and
the spoken welcome. **Never globalize the satellite settings.**

Normal log noise from room `speaker`: "current speech generation cannot be
interrupted" (someone spoke over a reply — expected under half-duplex) and an
occasional "STT stream ended … recreating" (recovered automatically).

**"Wake word not working"**: first check that room `speaker` exists and has
an `esp32` participant (healer log, or LiveKit `ListRooms`). No room means
the device is off the network, not a code problem. On 2026-10-04 a fan near
the device broke its 2.4 GHz Wi-Fi for ~10 min (associate, then a WPA
handshake timeout, repeated); it survived a power-cycle and cleared the
moment the fan was off. The firmware retries Wi-Fi and LiveKit forever, so
once the cause goes away it rejoins by itself. Off the network the UDP log
is dead: read serial with DTR/RTS left asserted (opening the port with them
released resets the board). Diagnosis and recovery gaps:
`BUG-0001-satellite-offline-recovery.md`.

## The LCD display (removed)

The Waveshare 1.47" Touch LCD module was **defective**: the JD9853 panel
never answered its ID register over MISO and the backlight never lit, while
its touch controller ACK'd on I2C — a dead panel, not a wiring/firmware
fault. It was removed.

The board's *display path is fine* — a replacement panel should light up
with **no firmware changes**: `ui_init()` auto-detects the panel via the
touch controller on I2C and, if present, brings up the JD9853 + LVGL UI;
if absent (today) it runs headless on the LED ring. On a working panel the
firmware plays a boot beep, then a 1.5 s white flash, then the status UI —
an instant pass/fail. All the LCD/touch/LVGL code is still in `main/ui.c`
and the drivers under `components/`.

## Restoring full duplex (barge-in) — the path

Barge-in was turned off because the board has **no working on-device echo
cancellation**, so with full-duplex Minny hears herself and loops. Restoring
it is a real task, not a toggle. In order:

1. **Verify the echo-reference channel map (prerequisite).** The board wires
   the ES8311 DAC output back into an ES7210 input (MIC3 per schematic v1.1)
   as a hardware AEC reference. This mapping was **never confirmed
   empirically**. Capture the raw 4-channel ES7210 stream and identify which
   TDM slot carries the mic (responds to voice) vs the loopback (responds to
   speaker playback). Everything below depends on getting this right.
2. **Bring back the AFE without the deadlock.** The AFE (`aec_src`) is what
   deadlocked the pipeline (JTAG-confirmed). Before re-enabling it, find why:
   candidates are task stack sizes, the `AFE_MODE`/config, or the
   `esp_capture`/esp-sr version. Re-enable `esp_capture_new_audio_aec_src`
   with the correct `channel` / `channel_mask` / `mic_layout` from step 1.
3. **Re-solve the audio-bus geometry.** 4-slot TDM capture (needed to reach
   the reference channel) forces 32-bit I2S slots, which silenced the DAC.
   Either find a config where playback survives 32-bit slots (try
   `use_mclk=true` again *with* the AFE present and measure with the boot
   beep + a delayed runtime tone), or confirm the reference can be captured
   within the 2-slot geometry.
4. **Drop the agent-side crutches for room `speaker`** once hardware AEC
   works: remove the `interruption:{enabled:False}` half-duplex block and the
   post-speech cooldown in `voice_agent.py`. Room `voice` already shows the
   target full-duplex behavior — the goal is parity. Keep the wake gate and
   the STT recycle: those are independent of echo.

Constraint added since this path was written: capture now runs through the
gated-tee source (`wake.c`) with WakeNet in its read path on an internal-RAM
stack. Any AFE/AEC must slot into that same continuous pipeline — gating by
stopping/starting capture is known to silence playback, and the AFE's
original deadlock was on a PSRAM stack, so give it an internal one as well.

If on-device AEC proves impractical on this board, the fallback is to keep
half-duplex but tighten it (shorter cooldown, tune endpointing) — the
current experience is already usable, just not interruptible.

## Bring-up leftovers

Done since the last revision:
- `sdkconfig` and `dependencies.lock` are tracked (2026-10-04). The committed
  `sdkconfig` has the Wi-Fi SSID/password and LiveKit token stripped by the
  `esp32-secrets` clean filter; a new clone must set the filter up before
  committing it (README, Secrets).
- Spoken welcome re-gated to room `voice` only — the satellite stays silent
  on connect until its wake word fires (`e9ea045`).
- Boot test tone removed — it sounded identical to the wake chime and read as
  a reboot (`c397f67`).

Still in the tree (harmless; remove when convenient):
- `main/ui.c` **boot RGB self-test** (`led_self_test()`), plus the white
  flash on a working panel. Fine as a power-on check.
- `main/ui.c` 30 s **heap-watch** log — useful if instability returns.

## Open items

1. **Barge-in on the satellite** — the path above; step 1 (ES7210 loopback
   slot map) is the gate for everything else.
2. **Tighten half-duplex** if on-device AEC proves impractical: shorter
   post-speech cooldown, endpointing tuning.
3. **LiveKit deprecations** in `voice_agent.py` (`RoomInputOptions` →
   `RoomOptions`, `metrics_collected` → `session_usage_updated`). Harmless
   today; they will bite on the next livekit-agents upgrade — and so may the
   STT recycle, which touches plugin internals (it degrades to a logged
   no-op and warns at startup if they move).
4. **Replacement LCD panel** — no firmware work needed (see above).
5. **BUG-0001: offline diagnosis and recovery.** Record the reset reason
   and core dumps, show "Wi-Fi down" on the ring, add a LiveKit-stuck restart
   watchdog that only fires while Wi-Fi is up, and have the healer warn when
   the satellite has been gone for more than 5 min. See
   `BUG-0001-satellite-offline-recovery.md`.

## Key file map

```
main/board.c   I2C, TCA9555 expander, I2S, ES8311+ES7210, PA, wake chime
main/media.c   capture scheduler (internal-RAM AUD_SRC, 40 KB aenc_0) +
               av_render pipelines
main/wake.c    gated-tee capture source + WakeNet ("Hey Wanda"),
               wake_frame_ticks() liveness counter
main/room.c    LiveKit room lifecycle, reconnect, agent presence + state
               topic, wake/sleep messages (topic "session"), capture watchdog
main/ui.c      panel auto-detect; JD9853+AXS5106+LVGL UI or headless LED
main/led.c     7x WS2812 ring status (RGB order)
main/net_log.c UDP log sink for USB-free debugging
sdk-patches/   local patch to the LiveKit SDK clone (subscriber data channel)
```
Backend contract and the wider voice stack: `dev/hermes/VOICE-AGENT.md`.
Build/flash and hardware pinout: `dev/voice/esp32/README.md`.
