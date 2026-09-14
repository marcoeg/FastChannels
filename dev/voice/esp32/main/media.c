// Capture and render pipelines, from the LiveKit custom_hardware example.
//
// Capture: esp_capture AEC source wraps the ES7210 record device and runs
// the esp-sr AFE (low-cost mode) for echo cancellation, producing 16 kHz
// mono 16-bit for the Opus encoder. On the ESP32-S3-AUDIO-Board the ES7210
// TDM wire order is slot0=MIC1, slot1=MIC3 (the hardware loopback of the
// ES8311 output), so channel_mask 1|2 captures exactly [mic, reference] via
// the I2S hardware slot mask and the AFE's default "MR" layout applies.

#include "esp_check.h"
#include "esp_log.h"
#include "av_render_default.h"
#include "esp_audio_dec_default.h"
#include "esp_audio_enc_default.h"
#include "esp_capture_defaults.h"
#include "esp_capture_sink.h"

#include "board.h"
#include "media.h"

static const char *TAG = "media";

#define NULL_CHECK(pointer, message) \
    ESP_RETURN_ON_FALSE(pointer != NULL, -1, TAG, message)

typedef struct {
    esp_capture_sink_handle_t capturer_handle;
    esp_capture_audio_src_if_t *audio_source;
} capture_system_t;

typedef struct {
    audio_render_handle_t audio_renderer;
    av_render_handle_t av_renderer_handle;
} renderer_system_t;

static capture_system_t  capturer_system;
static renderer_system_t renderer_system;

// Bring-up diagnostic: AUDIO_RENDERED fires on the FIRST incoming frame —
// its presence/absence splits "no audio arrives from the room" from
// "audio arrives but the codec write path is silent".
static int render_event_cb(av_render_event_t event, void *ctx)
{
    ESP_LOGI(TAG, "av_render event: %d%s", event,
             event == AV_RENDER_EVENT_AUDIO_RENDERED ? " (first audio frame arrived)" :
             event == AV_RENDER_EVENT_AUDIO_DECODE_ERR ? " (AUDIO DECODE ERROR)" : "");
    return 0;
}

static int build_capturer_system(void)
{
    esp_codec_dev_handle_t record_handle = get_record_handle();
    NULL_CHECK(record_handle, "Failed to get record handle");

    // Full 4-slot TDM capture is REQUIRED for echo cancellation: in
    // truncated 2-slot frames the ES7210 falls back to emitting its two
    // microphones (measured live: AEC then cancels the USER's voice —
    // speech at -59/-42 dBFS — and lets the agent's own TTS leak back as
    // user turns). The electrical DAC-loopback reference exists only in
    // the full stream: slot 0 = reference, slot 1 = MIC1, hence "RM".
    // The I2S hardware slot mask (1|2) captures just those two.
    // Playback stays audible at this 32-bit-slot geometry because the
    // ES8311 clocks from MCLK (see board.c).
    esp_capture_audio_aec_src_cfg_t codec_cfg = {
        .record_handle = record_handle,
        .channel = 4,
        .channel_mask = 1 | 2,
        .mic_layout = "RM",
    };
    capturer_system.audio_source = esp_capture_new_audio_aec_src(&codec_cfg);
    NULL_CHECK(capturer_system.audio_source, "Failed to create audio source");

    esp_capture_cfg_t cfg = {
        .sync_mode = ESP_CAPTURE_SYNC_MODE_AUDIO,
        .audio_src = capturer_system.audio_source,
    };
    esp_capture_open(&cfg, &capturer_system.capturer_handle);
    NULL_CHECK(capturer_system.capturer_handle, "Failed to open capture system");
    return 0;
}

static int build_renderer_system(void)
{
    esp_codec_dev_handle_t render_device = get_playback_handle();
    NULL_CHECK(render_device, "Failed to get render device handle");

    i2s_render_cfg_t i2s_cfg = {
        .play_handle = render_device,
    };
    renderer_system.audio_renderer = av_render_alloc_i2s_render(&i2s_cfg);
    NULL_CHECK(renderer_system.audio_renderer, "Failed to create I2S renderer");

    av_render_cfg_t render_cfg = {
        .audio_render = renderer_system.audio_renderer,
        .audio_raw_fifo_size = 8 * 4096,
        .audio_render_fifo_size = 100 * 1024,
        .allow_drop_data = false,
    };
    renderer_system.av_renderer_handle = av_render_open(&render_cfg);
    NULL_CHECK(renderer_system.av_renderer_handle, "Failed to create AV renderer");
    av_render_set_event_cb(renderer_system.av_renderer_handle, render_event_cb, NULL);

    av_render_audio_frame_info_t frame_info = {
        .sample_rate = 16000,
        .channel = 2,
        .bits_per_sample = 16,
    };
    av_render_set_fixed_frame_info(renderer_system.av_renderer_handle, &frame_info);
    return 0;
}

int media_init(void)
{
    esp_audio_enc_register_default();
    esp_audio_dec_register_default();

    if (build_capturer_system() != 0 || build_renderer_system() != 0) {
        ESP_LOGE(TAG, "Media pipeline init failed");
        return -1;
    }
    return 0;
}

esp_capture_handle_t media_get_capturer(void)
{
    return capturer_system.capturer_handle;
}

av_render_handle_t media_get_renderer(void)
{
    return renderer_system.av_renderer_handle;
}
