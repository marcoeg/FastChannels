#pragma once

#ifdef __cplusplus
extern "C" {
#endif

// Create the LiveKit room object and start the connection-manager task,
// which connects to CONFIG_LK_SERVER_URL with CONFIG_LK_TOKEN and
// reconnects with backoff whenever the room drops to DISCONNECTED/FAILED.
void room_start(void);

#ifdef __cplusplus
}
#endif
