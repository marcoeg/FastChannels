# Minny satellite — ESP32-S3 voice terminal

Firmware for the **Waveshare ESP32-S3-AUDIO-Board** + **Waveshare 1.47" Touch
LCD** (on the board's 18-pin LCD FPC / 2×9 header). The device joins the
self-hosted LiveKit room **`speaker`** over plain `ws://` as a full audio
participant: it publishes the on-board mic array (with hardware-referenced
echo cancellation) and plays Minny's replies through the speaker. The
touchscreen shows connection + agent state (listening / thinking / speaking)
and provides volume, speaker mute, and a touch-to-talk mic toggle.

It gets its **own room** (not `voice`) deliberately: the ESP32 SDK
subscribes to exactly one remote audio track, so sharing a room with the
phone/browser could latch the satellite onto the phone's microphone instead
of the agent's voice. The worker auto-dispatches to any room, so `speaker`
gets its own Minny session (with the shared memory graph).

```
mics (ES7210, AEC ref from ES8311 loopback) ──► esp-sr AFE ──► Opus 16k mono ─┐
                                                                              ├─ LiveKit room "speaker" ◄── ws://192.168.50.17:7880
speaker (ES8311 → NS4150B, EXIO8 enable)   ◄── Opus decode ◄──────────────────┘
LCD (JD9853 172x320 @ SPI3) + touch (AXS5106 @ I2C0 0x63) — LVGL 9 status UI
```

The voice agent itself stays on the backend (`dev/voice/voice_agent.py`).
It pushes its state to the device over the `agent_state` data topic (see the
`agent_state_changed` hook in voice_agent.py) because the ESP32 SDK cannot
read the `lk.agent.state` participant attribute (protocol v1).

## Prerequisites

- ESP-IDF **v5.5.1+** installed at `~/esp/esp-idf` (v5.5.5 in use).
- LiveKit ESP32 SDK clone at `~/esp/client-sdk-esp32`, pinned to `e0616ef`
  (main). The registry release 0.3.10 is NOT used on purpose: its vendored
  nanopb ~0.4.9 fails to skip ignored fields in SignalResponse from
  self-hosted livekit-server (SDK issue #86); main carries nanopb 0.4.9.2.
  `main/idf_component.yml` points at the clone via `override_path`.
- Secrets: `./gen_secrets.py --ssid "<2.4GHz SSID>" --password "<pass>"`
  mints a 365-day room token (identity `esp32`, room `speaker`) from
  `../.env`'s LiveKit keys and writes `sdkconfig.secrets.defaults`
  (git-excluded). **The ESP32-S3 is 2.4 GHz only** — "Final Foods_5G" won't
  work, use the 2.4 GHz SSID.

## Build / flash

```bash
./gen_secrets.py --ssid "..." --password "..."   # once (token: yearly)
./build.sh build
./build.sh flash monitor          # board on /dev/ttyACM0 (USB 303a:1001)
```

Flashing notes (board has native USB-Serial-JTAG only, no UART bridge):

- If the board doesn't enumerate (`lsusb` shows no `303a:`, kernel logs
  "Device not responding to setup address, error -71"): hold **BOOT**, tap
  **RESET**, release BOOT — ROM download mode always enumerates. A blank or
  boot-looping flash makes the USB port unstable (Waveshare FAQ); a
  power-only USB-C cable or flaky hub does the same. Prefer a root port.
- Serial access needs dialout membership:
  `sudo usermod -aG dialout $USER` + re-login (or a udev rule).

## What's on the screen

- Top bar: `MINNY` + connection status (wi-fi / connecting / online /
  reconnecting / offline).
- Orb (big circle) = agent state, colors matching the browser client:
  green listening, amber thinking, blue speaking, slate idle, red muted.
  **Tap the orb** to mute/unmute the mic (touch-to-talk).
- Bottom bar: speaker mute toggle, volume −/+ (persisted in NVS).

## Hardware map (schematic v1.1, verified against Waveshare demo code)

| Function | Pin |
| --- | --- |
| I2C0 (codecs, TCA9555 @0x20, RTC @0x51, touch @0x63) | SCL=GPIO10, SDA=GPIO11 |
| I2S1 (both codecs, full duplex) | MCLK=12, BCLK=13, WS=14, DIN=15 (ES7210), DOUT=16 (ES8311) |
| ES8311 DAC / ES7210 4-ch ADC | I2C 0x18 / 0x40 (7-bit) |
| Speaker PA (NS4150B) enable | TCA9555 **EXIO8**, active high |
| LCD (JD9853, 172×320, 34-px gap) | SPI3: SCK=4, MOSI=9, CS=3, DC=7; BL=GPIO5 (LEDC); RST=**EXIO0** |
| Touch (AXS5106, polled) | I2C0 0x63; RST=**EXIO1**; INT=EXIO2 (expander → no ESP interrupt) |
| Keys / RGB / SD | KEY1-3=EXIO9-11; 7×WS2812 GPIO38; SDMMC CLK=40 CMD=42 D0=41 |

AEC: the ES8311 output is looped through an attenuator into **ES7210 MIC3**
in hardware. On the TDM wire, slot0=MIC1 (mic) and slot1=MIC3 (reference),
so `media.c` captures slots 0+1 (`channel_mask = 1|2`) and the esp-sr AFE's
default `"MR"` layout applies. If AEC ever seems dead (agent hears itself),
that mapping is the first thing to re-check — Waveshare's demo reads the
same frames via 32-bit stereo slots and documents the byte-swapped view as
`"RMNM"`.

## Source layout

```
main/board.c   pins, I2C, TCA9555, I2S, ES8311+ES7210 (adapted from the
               LiveKit custom_hardware example; PMU dropped, PA via EXIO8)
main/media.c   esp_capture AEC source + av_render pipelines (stock example)
main/room.c    room lifecycle, reconnect loop, agent presence + state topic
main/ui.c      JD9853 + AXS5106 + LVGL 9 status screen (from Waveshare's
               lvgl9_3_example_04 init values)
components/    esp_lcd_jd9853, esp_lcd_touch_axs5106 — local components
               copied from Waveshare's demo (not on the component registry)
```

Two deliberate divergences from the `custom_hardware` example, found in
review of the IDF I2S driver:

- **Both I2S channels are initialized with one identical TDM config.** IDF
  only forms a true full-duplex pair (single clock on the shared BCLK/WS
  wires) when the two channels' configs match exactly; the example's
  TX-std + RX-TDM split leaves two master clocks fighting over the pins.
- **One `audio_codec_new_i2s_data` instance is shared by both codec
  devices**, so esp_codec_dev's cross-direction slot-width bookkeeping keeps
  playback and capture at the same 64-bit frame no matter which side opens
  first.

## Known limitations

- The SDK subscribes to **one** remote audio track — the reason the
  satellite lives in its own room. When the agent job restarts (daily 04:30
  worker recycle), the firmware closes and rejoins the room to pick up the
  new agent's track; expect a few seconds of "connecting" on the screen.
- No track-level mute in the SDK — mic mute is codec-level silence; the
  track stays "live" server-side and the agent's VAD just hears silence.
- Token is baked at build time; re-run `gen_secrets.py` + reflash yearly,
  or wire up a LAN token endpoint later. (Expired token = the UI stuck on
  "connection failed"; the firmware backs off to 60 s retries.)
- Camera FPC unused; EXIO7 (USB mux) must stay high or USB dies (default).
- When committing this directory, **never `git add -f` the whole tree** —
  that force-adds `sdkconfig`/`sdkconfig.secrets.defaults` (token + WiFi
  password) past both gitignores. Add files explicitly.
