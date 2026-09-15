// Capture and render pipelines, from the LiveKit custom_hardware example.
//
// Capture: PLAIN codec source (esp_capture_new_audio_dev_src) — raw PCM
// straight from the ES7210 to the Opus encoder, no esp-sr AFE.
//
// The AFE (acoustic front-end / echo canceller) was removed after a JTAG
// halt of a wedged board showed a hard deadlock in the media pipeline with
// the AFE's tasks (buffer_in / afe_fetch) in the stall and the whole system
// spinning on kernel locks. The agent runs half-duplex, so on-device echo
// cancellation is not needed; dropping the AFE removes both the deadlock
// suspect and an unused, heavy component. The dev_src opens the record
// device at the Opus format (16 kHz mono), which esp_codec_dev serves from
// slot 0 (MIC1) at the same 2x16-bit slot geometry that keeps playback
// audible on this shared full-duplex bus.

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

    esp_capture_audio_dev_src_cfg_t codec_cfg = {
        .record_handle = record_handle,
    };
    capturer_system.audio_source = esp_capture_new_audio_dev_src(&codec_cfg);
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

    // Small playout buffers on purpose. The SDK example uses 100 KB (~3 s at
    // 16 kHz mono) to ride out internet jitter, but on a 2 ms LAN that just
    // adds playout latency — and with no AFE, that latency IS the echo
    // window: the agent finishes "speaking" while 3 s of her audio is still
    // draining to the speaker, the hot mic hears it, and she transcribes
    // herself. ~0.5 s render / ~0.25 s raw keeps LAN playback smooth while
    // aligning "done speaking" with actual audio-out so half-duplex can
    // suppress the echo.
    av_render_cfg_t render_cfg = {
        .audio_render = renderer_system.audio_renderer,
        .audio_raw_fifo_size = 8 * 1024,
        .audio_render_fifo_size = 16 * 1024,
        .allow_drop_data = true,
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
