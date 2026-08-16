---
name: fastchannels-record
description: Record live TV from FastChannels to a file with record.py.
---

# Recording live TV — `record.py`

`record.py` captures a FastChannels channel to a file with ffmpeg. It is a
companion to the `fastchannels` skill: that one covers the HTTP API, this one
covers turning a channel into a recording on disk.

```
Script     /home/marco/Development/FastChannels/record.py
Repo root  /home/marco/Development/FastChannels
Output     ~/news/recordings           (i.e. /home/marco/news/recordings)
Server     http://127.0.0.1:5523       FastChannels, must be running
```

Run it by absolute path. It is a symlink to `dev/record.py` and works from any
working directory.

## When to use this

- "record", "capture", "save", "tape" a channel or a programme
- watching something later, keeping a copy, catching a show while away
- listing what has already been recorded, or what is scheduled

For "what is on", "what channels exist", or "give me a stream URL", use the
`fastchannels` skill instead — that is the API, and this tool sits on top of it.

## Scheduling: use your own cron, and call `now`

**Prefer your cron with `record.py now`.** Compute the start time yourself,
fire at that moment, and give `now` the duration. You keep visibility of what
is scheduled and can reason about it.

```
your cron fires at 18:00  ->  record.py now --channel "Al Jazeera" --duration 1h
```

`record.py at` exists and schedules through systemd instead (details below). It
is the right choice only when a recording must survive you being restarted or
unavailable, because systemd holds the timer independently.

**Do not use both for the same recording.** They do not know about each other,
and you will get two ffmpeg processes writing two files.

## Commands

### `now` — record immediately

Blocks until the recording finishes, then prints the file.

| Option | Required | Meaning |
| --- | --- | --- |
| `--channel NAME_OR_NUMBER` | yes | Channel name (substring, case-insensitive) or channel number |
| `--duration D` | yes | How long to record. See [duration formats](#duration-formats) |
| `--title TITLE` | no | Used in the filename. Defaults to the channel name |
| `--out DIR` | no | Output directory. Default `~/news/recordings` |
| `--mp4` | no | Remux to MP4 after a clean capture; the intermediate `.ts` is deleted |
| `--normalize` | no | Re-encode to a fixed 1280x720. Costs quality and CPU; rarely needed — see failure modes |
| `--no-pin` | no | Record the provider master instead of one rendition. **Loses video at advert breaks** — for reproducing that bug only |

```bash
/home/marco/Development/FastChannels/record.py now \
  --channel "Al Jazeera" --duration 1h --title "Newshour"
```

Output on success — path, size, and the duration actually captured:

```
/home/marco/news/recordings/Al-Jazeera_Newshour_2026-08-15_1800.ts  (412.7 MB, 3600.0s)
```

### `at` — schedule via systemd

Registers a transient systemd timer and returns immediately. The timer survives
logout and fires at boot if the machine was off, because lingering is enabled
for this account. Use this only when the schedule must outlive you; otherwise
use your own cron with `now`.

| Option | Required | Meaning |
| --- | --- | --- |
| `--channel NAME_OR_NUMBER` | yes | Channel name (substring, case-insensitive) or channel number |
| `--start WHEN` | yes | When to begin. See [time formats](#time-formats) |
| `--duration D` | yes | How long to record. See [duration formats](#duration-formats) |
| `--title TITLE` | no | Used in the filename **and** in the systemd unit name. Defaults to the channel name |
| `--out DIR` | no | Output directory. Default `~/news/recordings` |
| `--mp4` | no | Remux to MP4 after a clean capture |
| `--normalize` | no | Re-encode to a fixed 1280x720. Costs quality and CPU; rarely needed — see failure modes |
| `--no-pin` | no | Record the provider master instead of one rendition. **Loses video at advert breaks** — for reproducing that bug only |

```bash
/home/marco/Development/FastChannels/record.py at \
  --channel "Al Jazeera" --start "2026-08-16 18:00" --duration 1h --title "Newshour"
```

Prints the unit name, which is what `cancel` takes — **capture it if you may
need to cancel**:

```
scheduled fcrec-newshour-1786910400
  2026-08-16 18:00 PDT for 3600s -> /home/marco/news/recordings
```

Notes that matter:

- The unit name is `fcrec-<slug of title>-<unix start time>`. Two recordings
  with the same title and start time collide; vary the title.
- **The channel URL is resolved when the recording starts**, not when it is
  scheduled — FastChannels re-resolves `/play/…` per request and upstream
  session tokens expire, so scheduling far ahead is safe.
- Scheduling a start time in the past is accepted by systemd and fires
  immediately. Check `--start` before passing it on.

#### Time formats

`--start` accepts, all interpreted in the machine's local timezone (PDT):

| Form | Example | Result |
| --- | --- | --- |
| Relative | `+20min`, `+2h`, `+90s` | That much from now |
| Time only | `18:00`, `23:59:30` | Today at that time — **or tomorrow if it has already passed** |
| Date and time | `2026-08-16 09:15`, `2026-08-16 09:15:00` | Exactly that |

Anything else exits 1 with a message naming the accepted forms.

#### Duration formats

`--duration` accepts `45s`, `30m`, `1h`, `1h30m`, `2h15m`, `1d`, or a bare
number meaning seconds (`90` = 90 seconds). Combining units works; the parts
are summed.

### `programme` — schedule from the guide

Finds the next airing of a programme and schedules it, working out start and
stop from the EPG so you do not have to.

| Option | Required | Meaning |
| --- | --- | --- |
| `--channel NAME_OR_NUMBER` | yes | Which channel to search |
| `--match TITLE` | yes | Substring of the programme title, case-insensitive |
| `--pad-start SEC` | no | Start early. Default `30` |
| `--pad-end SEC` | no | Run late. Default `120` |
| `--dry-run` | no | Resolve and print, schedule nothing |
| `--json` | no | Machine-readable output |
| `--out DIR`, `--mp4`, `--normalize`, `--no-pin` | no | As above |

Always `--dry-run --json` first when acting on someone's behalf — it shows
which airing was chosen and how many others matched:

```json
{
  "channel": "Al Jazeera",
  "title": "Newshour",
  "airs": "2026-08-15T14:00:00-07:00",
  "record_from": "2026-08-15T13:59:30-07:00",
  "seconds": 3750,
  "file": "Al-Jazeera_Newshour_2026-08-15_1359.ts",
  "upcoming_matches": 40
}
```

If the programme is already on air it records the remainder immediately rather
than refusing.

### `epg` — what is on

| Option | Required | Meaning |
| --- | --- | --- |
| `--channel NAME_OR_NUMBER` | yes | Which channel |
| `--limit N` | no | How many upcoming entries. Default `20` |
| `--json` | no | Machine-readable, with ISO timestamps |

Guide data runs about five days ahead. Use this to pick a `--start` and
`--duration` for your own cron.

### `list` / `cancel` — scheduled recordings

`list [--json]` shows only timers created by this tool (`fcrec-` prefix);
`cancel UNIT` drops one, with or without the prefix and with or without the
`.timer` suffix. Both only ever see systemd timers — **recordings you schedule
with your own cron will not appear here**, because systemd does not know about
them.

## Channel matching

`--channel` takes a number (exact) or a name (substring, case-insensitive). An
exact name match always wins. Otherwise, if several channels match, it uses the
first and prints the alternatives to stderr — so a vague name silently records
the wrong thing. Verify with `epg --channel` first when the name is not exact.

## Results, and how to check them

Every run writes a sidecar next to the recording:

```
~/news/recordings/.meta/<name>.json     what happened
~/news/recordings/.meta/<name>.log      ffmpeg's stderr
```

The JSON carries `channel`, `url`, `requested_seconds`, `captured_seconds`,
`exit_status`, `timed_out`, `complete`, `pinned`, `video_at_end`, `normalized`,
`file` and `bytes`.

**Two fields decide whether a recording is good, and they catch different
failures:**

- **`complete`** — the capture ran to the requested length without stalling.
- **`video_at_end`** — there is still *video* in the last ten seconds. A
  recording can be `complete` and have no picture for most of its length (see
  below), so check both before telling anyone a recording succeeded.

If `video_at_end` is `false`, say so plainly. The audio is intact and the
opening stretch is watchable, but the picture stopped early.

A cut-short capture is kept, not discarded, and says so:

```
Al-Jazeera_Newshour_….ts  (180.2 MB, 1620.0s)  [PARTIAL: stream stalled, terminated at the deadline]
```

Report partials as partial. The file is usually watchable, but it is not what
was asked for.

## Limits and failure modes

**Exit code 1 with a message on stderr** for: no channel matched, no upcoming
programme matched, an unparseable `--start` or `--duration`, `ffmpeg` missing,
FastChannels unreachable, or a capture that produced no bytes at all.

**Adverts used to destroy recordings, and the fix is automatic — do not
disable it.** Ad-stitched channels (Pluto and similar) splice adverts at a
different encoding. Handed the provider *master* playlist, ffmpeg sees one
program per rendition and loses the mapped video stream at the first splice,
never re-acquiring it. Measured on 15-minute captures of the same channel:

```
master   900.0s container, 148.0s video  =  16%   (audio full length — looks fine)
pinned   900.0s container, 890.1s video  =  99%
```

`record.py` resolves the highest single rendition before capturing, which
leaves ffmpeg one audio and one video stream and nothing to lose. The sidecar
records `pinned: true`. **`--no-pin` exists only to reproduce the old failure
— never pass it for a real recording.**

`video_at_end` remains as a safety net, because pinning is unavailable when a
channel's `/play` URL does not resolve to an HLS master.

**`--normalize` is rarely what you want.** It re-encodes to a fixed 1280x720
and costs quality and CPU. It does *not* fix the advert problem — a scale
filter cannot process frames the demuxer stopped delivering — and pinning
already handles that. Reach for it only if a specific recording is unplayable.

**DRM channels cannot be recorded.** Roughly 69 of the lineup need a Widevine
bridge; ffmpeg gets nothing usable. Nothing detects this in advance — you find
out from a failed or empty capture.

**Recordings cannot overrun their deadline.** A stalled stream is terminated at
`duration + max(60s, duration/4)`, and whatever was captured is kept.

**Nothing throttles concurrent recordings.** Each is an independent ffmpeg
process. Several at once is fine; dozens will saturate the network, and
hammering one provider can trip its rate limiting — Roku answers a `403` with a
source-wide five-minute cooldown.

**Disk is not checked.** Budget about **2 GB per hour** — measured across real
1080p h264/AAC captures on this lineup, which came out at 1.9–2.1 GB/hour.
Nothing warns before filling the disk.
