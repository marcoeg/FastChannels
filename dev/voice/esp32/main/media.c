// Capture and render pipelines, from the LiveKit custom_hardware example.
//
// Capture source: the GATED WAKE-WORD source (wake_create_source, main/wake.c)
// wrapping the ES7210 — raw PCM straight to the Opus encoder, no esp-sr AFE.
// It runs WakeNet on the mic and publishes SILENCE until "Hey Wanda" is heard,
// then the real mic. Capture itself runs continuously (the LiveKit engine
// starts/stops it on connect/disconnect); gating is pure software, so the
// shared full-duplex I2S bus is never disturbed and playback stays audible.
//
// The AFE (acoustic front-end / echo canceller) was removed after a JTAG
// halt of a wedged board showed a hard deadlock in the media pipeline with
// the AFE's tasks (buffer_in / afe_fetch) in the stall and the whole system
// spinning on kernel locks. Root cause: esp-sr ran on esp_capture's default
// 4 KB PSRAM thread stacks (CAPTURE_DEFAULT_SCHEDULER). WakeNet now runs in
// the AUD_SRC read path, so capture_scheduler() below pins that one thread to
// an internal-RAM stack — the Waveshare-proven condition that runs esp-sr
// stably. The record device is opened at the Opus format (16 kHz mono), which
// esp_codec_dev serves from slot 0 (MIC1) at the 2x16-bit slot geometry that
// keeps playback audible on this shared bus.

#include <string.h>
#include "esp_check.h"
#include "esp_log.h"
#include "av_render_default.h"
#include "esp_audio_dec_default.h"
#include "esp_audio_enc_default.h"
#include "esp_capture.h"
#include "esp_capture_defaults.h"
#include "esp_capture_sink.h"

#include "board.h"
#include "media.h"
#include "wake.h"

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

// Thread scheduler for esp_capture. The LiveKit SDK installs its own
// (system.c) with stacks tuned for this pipeline — notably aenc_0 (Opus) needs
// 40 KB or it overflows and reboot-loops. Registering our own scheduler
// REPLACES the SDK's for every esp_capture thread, so we must reproduce those
// proven values here and change only what we need: AUD_SRC.
//
// AUD_SRC runs our gated source's read_frame, which calls WakeNet detect().
// esp-sr on a PSRAM stack is the AFE-class deadlock, so AUD_SRC moves to an
// INTERNAL-RAM stack (the SDK left it in PSRAM). WakeNet needs far less than
// the SDK's 40 KB; 12 KB internal is comfortable and internal RAM is scarce.
static void capture_scheduler(const char *name,
                              esp_capture_thread_schedule_cfg_t *cfg)
{
    if (name == NULL) {
        return;
    }
    if (strcmp(name, "AUD_SRC") == 0) {
        cfg->stack_in_ext = 0;          // internal RAM — required for esp-sr
        cfg->stack_size = 12 * 1024;
        cfg->core_id = 1;
        cfg->priority = 15;
    } else if (strcmp(name, "aenc_0") == 0) {
        cfg->stack_in_ext = 1;          // matches the SDK's proven Opus config
        cfg->stack_size = 40 * 1024;
        cfg->core_id = 1;
        cfg->priority = 10;
    }
    // buffer_in/venc_0/Muxer are unused in this audio-only, AEC-free path.
}

static int build_capturer_system(void)
{
    esp_codec_dev_handle_t record_handle = get_record_handle();
    NULL_CHECK(record_handle, "Failed to get record handle");

    esp_capture_set_thread_scheduler(capture_scheduler);

    // Gated wake-word source: WakeNet on the mic + publish silence until woken.
    // Falls back to the plain codec source only if it can't allocate.
    capturer_system.audio_source = wake_create_source(record_handle);
    if (capturer_system.audio_source == NULL) {
        ESP_LOGW(TAG, "wake source unavailable — using plain codec source");
        esp_capture_audio_dev_src_cfg_t codec_cfg = {
            .record_handle = record_handle,
        };
        capturer_system.audio_source = esp_capture_new_audio_dev_src(&codec_cfg);
    }
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
