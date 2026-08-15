# Introducing the `fastchannels-record` skill to Hermes

Paste everything below the rule to onboard the agent. It assumes the
`fastchannels` skill is already installed. Deliberately short — the skill file
carries the detail; this exists to make the agent load it, understand where it
ends, and prove it works before recording anything.

---

You now have a second skill, `fastchannels-record`, alongside the
`fastchannels` one you already have. It documents a local utility, and it
refers to files in the same repository:

```
/home/marco/Development/FastChannels
```

**Resolve every path against that root** — it is not your working directory.
The skill file is
`/home/marco/Development/FastChannels/dev/hermes/RECORD-SKILL.md`. Read it
before recording anything.

**What it is.** `record.py` captures a FastChannels channel to a file with
ffmpeg — a straight copy, no re-encode. Run it by absolute path,
`/home/marco/Development/FastChannels/record.py`. Recordings land in
`/home/marco/news/recordings`.

**How the two skills divide.** `fastchannels` is the HTTP API — what channels
exist, what is on, how to build a stream URL. `fastchannels-record` turns a
channel into a file on disk. When a request is about *watching* or *querying*,
that is the first skill; when it is about *keeping a copy*, this one.

## Scheduling is yours

**Use your own cron, and call `record.py now`.** Work out the start time
yourself, fire at that moment, and pass `now` the duration:

```
your cron fires at 18:00  ->  record.py now --channel "Al Jazeera" --duration 1h
```

`record.py at` schedules through systemd instead. Reach for it only when a
recording has to survive you being restarted or unavailable — systemd holds
that timer independently of you.

**Never use both for the same recording.** They cannot see each other, and you
will end up with two ffmpeg processes writing two files. For the same reason,
`record.py list` shows *only* systemd timers: anything you scheduled yourself
will not appear there, so an empty list does not mean nothing is scheduled.

Five things to internalise before you record anything:

1. **A vague channel name records the wrong channel, silently.** `--channel`
   matches a substring; an exact name wins, but otherwise it takes the first of
   several matches and mentions the alternatives only on stderr. Confirm with
   `record.py epg --channel "<name>"` when the name is not exact.

2. **`complete` in the sidecar is the field to trust.** Every run writes
   `.meta/<name>.json` beside the recording. A stalled capture is deliberately
   *kept* rather than discarded, so a partial otherwise looks like a success.
   If `complete` is false, say so — do not report a partial as done.

3. **DRM channels cannot be recorded.** Roughly 69 of the lineup need a
   Widevine bridge that does not exist here. Nothing detects this in advance;
   you find out from a failed or empty capture.

4. **Recording costs about 2 GB per hour** and nothing checks free space.
   Before a long or bulk recording, check the disk and say what it will use.

5. **Do not fan out.** Each recording is an independent ffmpeg process with no
   throttling. A handful at once is fine; many at once saturates the network,
   and hammering one provider trips its rate limiting — Roku answers a `403`
   with a source-wide five-minute cooldown that breaks every Roku channel.

To confirm the skill loaded and the tool works, run these and report what you
get — the last one records a real 20-second file:

```bash
R=/home/marco/Development/FastChannels/record.py
$R --help | head -3
$R epg --channel "Al Jazeera" --limit 3
$R programme --channel "Al Jazeera" --match "News" --dry-run --json
$R list
$R now --channel "Al Jazeera" --duration 20s --title "hermes check"
```

Then tell me, in your own words: how you would record a programme that airs
tomorrow evening, how you would know afterwards whether it recorded properly,
and which of the two skills you would reach for to answer "what is on Al
Jazeera tonight".
