# Minny voice satellite — project status

_Last updated 2026-09-14._

## TL;DR

A Waveshare **ESP32-S3-AUDIO-Board** runs as a standalone voice terminal for
the Minny agent: it joins the self-hosted LiveKit room `speaker` over WiFi,
publishes its microphone, and plays Minny's replies through its speaker. It
holds good multi-turn conversations (EPG lookups, "what's on," plot
summaries, TV control) and is **stable** over long sessions.

**Working today**
- Standalone: powered from any 5V USB charger (no computer needed); WiFi
  creds + a 1-year LiveKit token are baked into flash. Needs the nucbox host
  (LiveKit server + `voice-agent` worker) up on the LAN. `loginctl` lingering
  is on, so the backend survives reboots unattended.
- Status shown on the board's 7-LED RGB ring: green = listening/idle,
  amber = thinking, blue = speaking, red = connection failed.

**Two accepted limitations**
1. **Half-duplex** — you must let Minny finish before speaking; no barge-in
   (see "Restoring full duplex" below). The phone/browser (room `voice`)
   is unaffected and keeps full barge-in.
2. **No LCD** — the 1.47" touchscreen module was dead on arrival and was
   removed; the LED ring is the status display instead.

## What was done, and why

The build was a chain of real hardware/SDK bugs, each fixed from evidence.
Commits `c4cdb17` → `2d0120b` on `origin` (private mirror).

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

**Debugging methods worth keeping** (in the tree):
- `main/net_log.c` — the device streams `esp_log` over UDP to
  `udp://<host>:5514`. Opening the USB console **resets** the board, which
  hid failures; the WiFi log is the non-invasive view.
- JTAG over the built-in USB: `openocd -f board/esp32s3-builtin.cfg`, then
  `xtensa-esp32s3-elf-gdb build/minny_satellite.elf` → `thread apply all bt`.
  Needs `sudo chmod 666 /dev/bus/usb/<bus>/<dev>` for the S3's USB-JTAG node
  (the node number changes on re-enumeration).

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
   target full-duplex behavior — the goal is parity.

If on-device AEC proves impractical on this board, the fallback is to keep
half-duplex but tighten it (shorter cooldown, tune endpointing) — the
current experience is already usable, just not interruptible.

## Temporary bring-up bits to revert when convenient

- `voice_agent.py`: the spoken welcome is **un-gated to room `speaker`** (was
  added to test playback end-to-end). Re-gate to `voice` only so the speaker
  doesn't greet on every reconnect/worker-recycle.
- `main/board.c` plays a **boot beep** (test tone) and `main/ui.c` does a
  **boot RGB self-test** + white flash. Fine to keep as a power-on chime /
  panel check, or remove for a silent boot.
- `main/ui.c` has a 30 s heap-watch log (harmless; useful if instability
  ever returns).

## Key file map

```
main/board.c   I2C, TCA9555 expander, I2S, ES8311+ES7210, PA, test tone
main/media.c   capture (dev_src, no AFE) + av_render pipelines
main/room.c    LiveKit room lifecycle, reconnect, agent presence + state topic
main/ui.c      panel auto-detect; JD9853+AXS5106+LVGL UI or headless LED
main/led.c     7x WS2812 ring status (RGB order)
main/net_log.c UDP log sink for USB-free debugging
sdk-patches/   local patch to the LiveKit SDK clone (subscriber data channel)
```
Backend contract and the wider voice stack: `dev/hermes/VOICE-AGENT.md`.
Build/flash and hardware pinout: `dev/voice/esp32/README.md`.
