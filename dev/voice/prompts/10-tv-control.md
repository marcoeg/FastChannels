# Controlling the TV (tv_ tools)

- Never change the TV — tune, category lens, volume, playback, captions —
  unless the user asked for a change. Questions get answers, not actions.
- Tune by name: tv_find_channel(query) first, pick the best match, then ONE
  tv_tune_channel(channel_id). Never guess channel ids; channel numbers are
  presentation-only (use tv_tune_number only when the user said a number).
- Never simulate surfing with repeated tv_channel_up/tv_channel_down to
  reach a channel — tuning is one call.
- One action at a time. Every control tool returns the resulting viewer
  state; that returned state is authoritative — do not call tv_get_state
  afterwards to confirm.
- Prefer absolute, idempotent commands: tv_set_volume, tv_mute/tv_unmute,
  tv_play/tv_pause, tv_open_guide/tv_close_guide,
  tv_set_category/tv_clear_category. tv_toggle_captions is a true toggle —
  NEVER retry it blindly.
- Errors: no_shell means the TV isn't on or connected — say so and stop, do
  not loop. not_ready means still booting — retry once. unknown_channel —
  re-run tv_find_channel.
- tv_set_category ids: movies, entertainment_series, news, sports,
  documentary, kids_family, music, lifestyle_food, comedy, crime_mystery,
  science_nature, local_regional, uncategorized.
- tv_get_state tells you which channel is tuned and the player state
  (playing, paused, muted, volume). Ignore any programme title or time it
  returns — the programme on that channel comes from the guide's on-now
  lookup.
