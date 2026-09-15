#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Called from the WakeNet task when the wake word ("Hey Wanda") is detected.
// The detector has already released the microphone by the time this fires, so
// the callback (or whatever it notifies) may immediately hand the mic to the
// publish path via esp_capture_start(). Keep it lightweight — it runs on the
// WakeNet task.
typedef void (*wake_detected_cb_t)(void *ctx);

// Load the WakeNet model and spawn the (initially paused) detector task.
// Must be called after board_init() (the record codec must exist). Returns
// ESP_ERR_NOT_FOUND if the model partition/model is missing.
esp_err_t wake_init(wake_detected_cb_t cb, void *ctx);

// Begin listening for the wake word — the detector OWNS the mic (opens the
// record device). Call when the device goes ASLEEP. No-op if already listening.
void wake_listen_start(void);

// Stop listening and RELEASE the mic (close the record device), so the
// publish path can open it. Blocks until the mic is actually free. Call
// before esp_capture_start(). No-op if not listening.
void wake_listen_stop(void);

#ifdef __cplusplus
}
#endif
