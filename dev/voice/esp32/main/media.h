#pragma once

#include "esp_capture.h"
#include "av_render.h"

#ifdef __cplusplus
extern "C" {
#endif

// Initialize the capture (microphone + AEC) and render (speaker) pipelines.
int media_init(void);

// Capturer handle for publishing audio to a LiveKit room.
esp_capture_handle_t media_get_capturer(void);

// Renderer handle for playing audio from a LiveKit room.
av_render_handle_t media_get_renderer(void);

#ifdef __cplusplus
}
#endif
