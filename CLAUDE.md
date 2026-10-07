# CLAUDE.md

Working notes for this checkout. **Private mirror only — never goes upstream.**
`CLAUDE.md` is in upstream's `.gitignore` (line 49) and absent from their tree,
so it needs `git add -f` on its first commit here and cannot reach the public
repo through the PR flow.

## What this checkout is

A private working copy of `kineticman/FastChannels` — a Flask FAST-channel
aggregator (~63k LOC) that scrapes free streaming services and emits M3U +
XMLTV for Channels DVR / Plex. Runs in Docker on port 5523.

```
origin    marcoeg/fastchannels-private   PRIVATE. Push here. `main` tracks origin/main.
upstream  kineticman/FastChannels        READ only. Never push. Pull reference.
fork      marcoeg/FastChannels           PUBLIC fork. Only for upstream PR branches.
```

`origin` is **not** a GitHub fork — it was created empty and pushed, so it sits
outside upstream's fork network and can be private. That is also why PRs cannot
originate from it; they need `fork`.

## Hard rules

- **Never push to `upstream`.** Read-only access; contributions go through `fork`.
- **Never let these reach `fork` or upstream:** `CLAUDE.md`, `README-MEG.md`,
  `dev/`, `docker-compose.override.yml`, `docs/API.md`, `docs/openapi.yaml`,
  and the `app/worker.py` yt-dlp gate. An upstream PR branch is built from
  `upstream/main` and takes only the specific files being contributed.
- **Never commit exports.** `news*.csv` / `news*.json` are excluded in
  `.git/info/exclude` — they are regenerable and their `remote_url` values
  carry provider session tokens.
- **Verify before committing.** Especially for `app/` changes, which need a
  rebuild to take effect at all (see below).

## Git — committing to the private mirror

```bash
# 1. if app/ changed, rebuild first — the container has a COPY, not a bind mount
sudo docker compose up -d --build

# 2. review scope
git status --short
git diff

# 3. stage, grouped by concern (one commit per concern)
git add <paths>

# 4. commit: subject, blank line, body explaining WHY
git commit

# 5. push to the private mirror
git push                      # bare push is correct: main -> origin/main
```

`dev/`, `docker-compose.override.yml` and `CLAUDE.md` are gitignored
upstream, so **every** `git add` of a path under them needs `-f` — this
git (2.43) refuses an explicit path inside an ignored directory even when
the file is already tracked ("The following paths are ignored by one of
your .gitignore files"). `git commit -a` and `git add -u` still pick up
tracked files there without `-f`.

Commit messages end with:

```
Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
```

Keep unrelated changes in separate commits — it is what makes the local
divergences below individually revertable when upstream conflicts.

## Git — contributing upstream via PR

Only for genuine upstream bug fixes, never for local preferences.

```bash
# 1. verify the fix works against a running instance FIRST
sudo docker compose up -d --build
# …exercise the affected endpoint, confirm the fix and check for regressions

# 2. branch from upstream, not from main
git fetch upstream
git checkout -b fix/<short-name> upstream/main

# 3. take ONLY the files being contributed
git checkout <local-sha> -- app/scrapers/<file>.py
git diff --cached --stat          # must list exactly the intended files

# 4. commit and push to the PUBLIC fork
git commit
git push fork HEAD

# 5. open the PR
gh pr create --repo kineticman/FastChannels \
  --base main --head marcoeg:fix/<short-name> \
  --title "…" --body-file <path>

# 6. back to local work
git checkout main
```

PR body should carry: symptom, the real `curl -D-` output showing the failure,
root cause, the fix and its guards, and a before/after repro. Upstream has no
`CONTRIBUTING.md`, no PR template, no `LICENSE` and no test suite, and has
merged exactly one external PR — so a self-verifying body matters more than
usual.

Open PR: **#39** (xumo JSON playlist indirection). First-time-contributor
runs sit at `action_required` until a maintainer approves them; that is not a
failure.

`gh pr view --comments` is broken on gh 2.45.0 (deprecated `projectCards`
GraphQL field). Use `--json` field selection, or
`gh api repos/kineticman/FastChannels/issues/<n>/comments`.

## Taking upstream changes

```bash
git fetch upstream
git merge upstream/main
git push
```

Two tracked files diverge, each with a saved patch under `dev/`:

| File | Change | Patch |
| --- | --- | --- |
| `app/worker.py` | `FC_YTDLP_AUTO_UPGRADE` gate — local preference, stays local | `dev/ytdlp-gate.patch` |
| `app/scrapers/xumo.py` | JSON playlist indirection — submitted as PR #39 | `dev/xumo-indirection.patch` |

On conflict, take upstream wholesale and re-apply:

```bash
git checkout --theirs app/worker.py app/scrapers/xumo.py
git apply -3 dev/ytdlp-gate.patch dev/xumo-indirection.patch
```

If PR #39 merges, drop `dev/xumo-indirection.patch` and take upstream's version.

## This install

- **Port is published on all interfaces** (`5523:5523`, set in
  `docker-compose.override.yml` with a `!override` tag — Compose appends to
  list fields by default), so the LAN can reach it. This was a deliberate
  choice, made knowing the consequence: the app has **no authentication on any
  route**, including destructive ones, and two endpoints are unauthenticated
  SSRF. Anything that can route to this host has full admin. `ufw` does not
  help — Docker's iptables rules sit ahead of ufw's INPUT chain, so a
  published port bypasses it; restricting access would need a `DOCKER-USER`
  rule or an authenticating reverse proxy.
- **`public_base_url` is `http://nucbox.local:5523`**, not loopback, so the
  M3U and XMLTV outputs hand LAN clients URLs they can actually reach. Both
  halves are needed: opening the port alone leaves every playlist entry
  pointing at the client itself. Precedence is DB column →
  `FASTCHANNELS_SERVER_URL` → `PUBLIC_BASE_URL` (`app/models.py:450`), and the
  DB column is set, so changing the env vars alone would do nothing.
- `VERSION_CHECK_ENABLED=0` and `FC_YTDLP_AUTO_UPGRADE=0` are set.
- **`app/` has no bind mount.** Editing source on the host does nothing until
  `docker compose up -d --build`. `COPY . .` is late in the Dockerfile, so only
  the last layers rebuild — it is fast, not a cold build.
- Local tooling lives in `dev/` and is documented in `README-MEG.md`;
  `docs/openapi.yaml` + `docs/API.md` document the HTTP API (182 paths,
  verified against the route handlers).

## API gotchas that have caused real bugs

- **`Channel.to_dict()` omits `source_channel_id`**, so `/api/channels` alone
  cannot build a `/play/{source}/{id}.m3u8` URL. Get it from the M3U outputs or
  `/api/channels/{id}/preview`.
- **Category filters differ between surfaces.** `/api/channels` does a
  substring `LIKE` (so `News` also returns `Local News`); the `/m3u` endpoints
  match **exactly**. `News` and `Local News` are distinct categories.
- **Two playlists, always.** Channels with a Gracenote ID are partitioned into
  `/m3u/gracenote` and are absent from `/m3u`. Fetching one silently loses them.
- **Two different `503`s.** `Retry-After: 15` + "artifact is warming" means the
  background worker is still building output. `Retry-After: 30` + "Stream
  temporarily unavailable" means stream resolution failed — the real cause is in
  `/api/logs`, matched on the `X-FastChannels-Request-ID` response header.
- **Resolving remote URLs is not read-only.** Following `/play` runs the
  server's real health check, which can **auto-disable** channels it finds
  DRM-protected or dead (29 Roku channels were disabled this way once). Use
  `--no-resolve` in `dev/export_channels.py` for a read-only run. Prefer
  `local_url` for playback anyway — remote URLs carry expiring session tokens,
  some are IP-bound, and 21 of 79 News channels have no remote URL at all
  because their source is proxied by design.
- **Roku rate-limits.** A `403` triggers a source-wide five-minute cooldown
  during which every Roku channel returns `503`. Retrying makes it worse.
