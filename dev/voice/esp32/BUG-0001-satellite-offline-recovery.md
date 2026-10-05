# BUG-0001 — Satellite goes offline with no diagnosis and no targeted recovery

| | |
|---|---|
| Component | ESP32-S3 satellite firmware (`dev/voice/esp32`), plus host-side monitoring |
| Severity | Medium: the satellite is silently unusable ("wake word not working") until someone works out why |
| Status | Open; proposal below, not implemented |
| Found | 2026-10-04 |

## Summary

On 2026-10-04 the satellite dropped out of LiveKit room `speaker` and stayed out
for about 10 minutes. From the user's side the symptom was only "the wake word
is not working". Finding the cause took a LiveKit server log, a kernel USB log
and a serial console, and the cause turned out to be **Wi-Fi interference from
a fan near the device**. Switching the fan off fixed it at once.

The first hypothesis, recorded in the session, was that the firmware should
"do a full restart, including the audio codec, if it stays off LiveKit for a
couple of minutes after boot". The evidence below shows that **this would not
have helped here**. The firmware already retries Wi-Fi and LiveKit forever and
recovered by itself once the interference stopped, and a power-cycle (a far
stronger reset) did not help while the fan was on. What the incident exposed is
different:

1. Nobody could tell it was a Wi-Fi problem: the LED ring doesn't show Wi-Fi
   state after boot.
2. An unexplained reboot (18:28) left no trace: the reset reason isn't
   recorded, core dumps are off, and the UDP log needs the very Wi-Fi that was
   down.
3. Nothing on the host notices that the satellite has been gone for minutes.
4. A restart watchdog is still worth having for one case it *would* fix:
   **Wi-Fi up but LiveKit stuck**. It must not fire while Wi-Fi is down,
   where restarting cannot help.

## Incident timeline (2026-10-04, Pacific)

| Time | Event | Source |
|---|---|---|
| 18:26:02 | Voice agent restarted; the healer deletes the orphaned `speaker` room (by design) | LiveKit log |
| 18:26:03 | Satellite (`esp32`) reconnects within a second; new agent joins; works | LiveKit log |
| 18:26:47 | "Short ICE connection" on both satellite transports | LiveKit log |
| 18:26:53 | Satellite leaves: `PEER_CONNECTION_DISCONNECTED` | LiveKit log |
| 18:27:13 | Empty room closed (departure timeout); agent job exits | LiveKit / agent log |
| 18:28:00 | Chip reboots (USB re-enumerates); **cause unknown**, no log survived | kernel log |
| 18:28 → 18:36 | Device off the network: no ping, no ARP entry, no LiveKit connect attempts | ping, `ip neigh`, LiveKit log |
| 18:32:00 | User power-cycles the device; still no network | kernel log |
| ~18:35 | Serial console: Wi-Fi associates with "Final Foods" (2.4 GHz ch 9), then drops 0.5–10 s later, over and over (`run -> init (0xcc00)`, reason 204 `HANDSHAKE_TIMEOUT`, plus others); `network_connect: Retry: attempt=10` and counting | serial |
| 18:36:58 | Fan switched off → Wi-Fi holds → satellite rejoins `speaker`; agent puts it to sleep at 18:37:30; wake word works | LiveKit / agent log |

The password was correct: the device fully connected (`connected with Final
Foods, aid = 19`) between drops. The firmware binary dates from 2026-09-15 and
`sdkconfig` on disk was untouched; no build or flash was involved.

Side finding while diagnosing: opening `/dev/ttyACM0` with pyserial
`dtr=False, rts=False` reset the board once. Leaving both lines asserted
(pyserial's default, like `cat`) read without a reset.

## What the firmware does today

- **Wi-Fi.** `lk_example_network_connect()` (SDK `example_utils`) calls
  `esp_wifi_connect()` again on every `WIFI_EVENT_STA_DISCONNECTED`, with no
  backoff. `CONFIG_LK_EXAMPLE_NETWORK_MAX_RETRIES=-1` means forever, so the
  give-up branch in `main/main.c:64` (`UI_CONN_WIFI_FAILED`, then `app_main`
  returns, with no network until power-cycled) is unreachable with the current
  config, but would be a dead end if the value ever changed.
- **LiveKit.** `room_task` (`main/room.c`) reconnects whenever the room is
  `DISCONNECTED`/`FAILED`: 5 s backoff, 60 s for auth failures. It also closes
  and reconnects when the agent leaves, to resubscribe to the next agent job.
- **Capture watchdog.** `capture_health_task` reboots after 30 s without a
  captured frame, **only while connected**.
- **Codec on reboot.** `board_init()` re-creates the ES8311/ES7210 codec
  handles and re-programs them over I2C on every boot. The ESP side has no
  codec power switch; `EXIO_PA_EN` only gates the speaker amp. A chip restart
  therefore already does everything except a power cycle.
- **LED ring.** Shows Wi-Fi state only during boot
  (`UI_CONN_WIFI_CONNECTING`/`FAILED`). A later Wi-Fi drop shows as the
  LiveKit "connecting/reconnecting" state, the same as a server-side problem.
- **Diagnostics.** esp_log goes over UDP to `192.168.50.17:5514`
  (`net_log.c`), started only after the network is up. `esp_reset_reason()`
  is not logged. Core dumps are off (`CONFIG_ESP_COREDUMP_ENABLE_TO_NONE=y`).

## Proposed fixes (in priority order)

### 1. Record why the device rebooted

- At boot, read `esp_reset_reason()` and keep it, with a boot counter in RTC
  memory or NVS. Log it once `net_log` is up, so it reaches the UDP log.
- Enable core dump to flash (`CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH`, with a
  partition), so a panic like the 18:28 reboot leaves a backtrace readable
  with `idf.py coredump-info`.

*Why first:* the 18:28 reboot is the one unexplained event in the incident,
and every future one will be too until this exists.

### 2. Show Wi-Fi state on the LED ring after boot

Register a handler for `WIFI_EVENT_STA_DISCONNECTED` / `IP_EVENT_STA_GOT_IP`
in the firmware (not the SDK helper) and post a distinct UI state for
"Wi-Fi down", different from "LiveKit reconnecting". Also log RSSI on connect
and the disconnect reason code.

*Why:* this one signal would have pointed at RF interference in seconds.

### 3. LiveKit-stuck watchdog: restart only when Wi-Fi is up

If the device **has an IP** but LiveKit has not been `CONNECTED` for N minutes
(start with 5), call `esp_restart()`.

- Never fire while Wi-Fi is down: restarting cannot fix RF, an AP outage or a
  wrong password.
- Back off across reboots (count in RTC memory: 5, 10, 20 min, …), so a
  server outage doesn't turn into a reboot loop.
- Log the reason before restarting (see 1).

*What it fixes:* a wedged SDK or connection state that only a reboot clears.
*What it does not fix:* anything like this incident.

### 4. Host-side alert when the satellite is gone

`voice-room-healer.timer` already lists rooms every 2 minutes. Have it log a
warning, and optionally notify, when room `speaker` has had no `esp32`
participant for more than 5 minutes. That turns "the wake word is not
working" into a concrete "satellite offline since HH:MM", before anyone tries
to talk to it.

### Not proposed

- **"Full restart including the audio codec."** The firmware already
  re-initializes the codecs over I2C on every boot. A real codec power reset
  would need hardware the ESP doesn't control, and the power-cycle on
  2026-10-04 (which did reset everything) did not help.
- **Restart on Wi-Fi failure.** The driver retries continuously, and the
  device rejoined by itself once the interference stopped.

## Acceptance checks

- Unplug the AP (or move the device out of range): the ring shows "Wi-Fi
  down" within seconds; no restart; after the AP returns, the device rejoins
  `speaker` without intervention, and the healer has logged "satellite offline
  since …".
- Block LiveKit (stop `hermes-voice-livekit-1`) with Wi-Fi up: after N minutes
  the device restarts once, logs the reason over UDP, and backs off. No
  reboot loop over 30 minutes.
- Force a panic (test build): after reboot, `idf.py coredump-info` shows the
  backtrace, and the UDP log shows `reset reason: PANIC`.
- Normal operation (24 h soak): no watchdog restarts, wake word works
  throughout.

## Operator notes until this is fixed

- "Wake word not working": check whether room `speaker` exists before
  suspecting code (healer log, or LiveKit `ListRooms`). No room means the
  device is off the network.
- Off the network, the UDP log is dead and serial is the only view. Read it
  with the control lines left asserted (see the side finding above), or expect
  a reset.
- Check for nearby RF sources: the device is 2.4 GHz only, and a fan was
  enough to break it.
