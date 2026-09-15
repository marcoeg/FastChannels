#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_capture_audio_src_if.h"
#include "esp_codec_dev.h"

#ifdef __cplusplus
extern "C" {
#endif

// Called (on the capture thread) when the wake word is detected. Keep it
// lightweight — it should just kick the supervisor (chime + LED). The source
// is already AWAKE (publishing real audio) by the time this fires.
typedef void (*wake_detected_cb_t)(void *ctx);

// Create the gated capture source that wraps the record device: it publishes
// silence while asleep (running WakeNet on the real mic) and real audio once
// woken. Pass the result as esp_capture's audio source. If the wake model is
// missing it runs always-awake. Returns NULL on allocation failure.
esp_capture_audio_src_if_t *wake_create_source(esp_codec_dev_handle_t record);

// Register the wake-detected callback (after wake_create_source).
void wake_set_detected_cb(wake_detected_cb_t cb, void *ctx);

// Set the gate: false = ASLEEP (publish silence + listen for wake word),
// true = AWAKE (publish real mic). Detection sets it true automatically; the
// supervisor sets it false after an idle conversation. No-op if ungated.
void wake_set_awake(bool awake);

// True if wake gating is active (a model loaded).
bool wake_is_gated(void);

// Monotonic count of capture frames read — bumped on every read_frame. Used by
// the capture health watchdog to detect a dead pipeline (a stalled count while
// connected means WakeNet has stopped running).
uint32_t wake_frame_ticks(void);

#ifdef __cplusplus
}
#endif
