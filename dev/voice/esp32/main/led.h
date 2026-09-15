#pragma once

#include <stdbool.h>
#include "ui.h"

#ifdef __cplusplus
extern "C" {
#endif

// 7x WS2812 ring on the board's back (GPIO38) — the primary status display
// while running headless (no LCD).
void led_init(void);

// Boot diagnostic: red / green / blue, 1.5 s each.
void led_self_test(void);

// Recompute and show the ring color from connection + agent state + mute.
void led_update(ui_conn_t conn, ui_agent_t agent, bool mic_muted);

// Standby: dim "waiting for wake word" indication.
void led_show_asleep(void);

#ifdef __cplusplus
}
#endif
