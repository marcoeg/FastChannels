#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// Connection-level status (WiFi + LiveKit room).
typedef enum {
    UI_CONN_BOOT = 0,
    UI_CONN_WIFI_CONNECTING,
    UI_CONN_WIFI_FAILED,
    UI_CONN_CONNECTING,
    UI_CONN_CONNECTED,
    UI_CONN_RECONNECTING,
    UI_CONN_FAILED,
} ui_conn_t;

// Agent presence / speech state. States beyond NONE come from the Python
// agent pushing lk.agent.state transitions over the "agent_state" data topic
// (the ESP32 SDK cannot read participant attributes).
typedef enum {
    UI_AGENT_NONE = 0,
    UI_AGENT_INITIALIZING,
    UI_AGENT_IDLE,
    UI_AGENT_LISTENING,
    UI_AGENT_THINKING,
    UI_AGENT_SPEAKING,
} ui_agent_t;

// Bring up SPI LCD (JD9853), touch (AXS5106), LVGL, and the status screen.
// Requires board_init() to have run (I2C bus + TCA9555 for the reset lines).
void ui_init(void);

// Thread-safe state updates (queued to the UI task; callable from any task).
void ui_post_conn(ui_conn_t state);
void ui_post_agent(ui_agent_t state);

// Standby indicator: when asleep, the ring shows a dim "waiting for wake word"
// state that overrides the agent-state colors (the agent keeps sending its
// state, but the device isn't listening to it). Callable from any task.
void ui_post_sleep(bool asleep);

#ifdef __cplusplus
}
#endif
