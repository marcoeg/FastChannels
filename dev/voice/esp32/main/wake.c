// On-device wake word ("Hey Wanda") as a GATED CAPTURE SOURCE.
//
// This is an esp_capture audio source that wraps the ES7210 record device — a
// byte-for-byte copy of the stock dev source (capture_audio_dev_src.c), whose
// geometry keeps playback audible on this shared full-duplex bus — with two
// additions in its read path:
//   1. it tees the real mic PCM into standalone WakeNet (esp_wn_iface) for
//      wake-word detection, and
//   2. it gates the PUBLISHED audio: while ASLEEP it returns SILENCE (so the
//      server's VAD sees nothing and no cloud STT runs); once woken it returns
//      the real mic.
//
// Why a source instead of stopping/starting capture: the record and playback
// codecs share ONE full-duplex I2S bus. Toggling the mic (esp_capture_stop/
// start) closes/reopens the record device, which disturbs the shared bus and
// SILENCES playback (observed: chime played, agent replies did not). Keeping
// esp_capture running continuously — exactly like the last known-good audio
// build — leaves the bus untouched, so playback works; gating is pure
// software. No mic hand-off, no single-open race.
//
// WakeNet runs standalone (no esp_afe) inside the "AUD_SRC" capture thread,
// which media.c's scheduler pins to an INTERNAL-RAM stack — esp-sr on a PSRAM
// stack is the exact condition that deadlocked the old AFE path.

#include "wake.h"

#include <stdlib.h>
#include <string.h>
#include "esp_capture_audio_src_if.h"
#include "esp_capture_types.h"
#include "esp_codec_dev.h"
#include "esp_log.h"
#include "esp_wn_iface.h"
#include "esp_wn_models.h"
#include "model_path.h"

static const char *TAG = "wake";

typedef struct {
    esp_capture_audio_src_if_t base;
    esp_codec_dev_handle_t     handle;
    esp_capture_audio_info_t   info;
    int                        frame_num;
    bool                       use_fixed_caps;
    bool                       started;
    bool                       opened;

    // WakeNet
    const esp_wn_iface_t      *wn;
    model_iface_data_t        *wn_data;
    int                        wn_chunk;    // samples per detect()
    int16_t                   *wn_accum;    // framing buffer (wn_chunk samples)
    int                        wn_fill;
    bool                       fired;       // detected, awaiting supervisor wake

    // Gate: false = ASLEEP (publish silence, run detector); true = AWAKE.
    volatile bool              awake;
    wake_detected_cb_t         cb;
    void                      *cb_ctx;
} gated_src_t;

static gated_src_t *s_src;   // singleton (one mic)

// Liveness counter: bumped on every read_frame. A stalled counter while the
// room is connected means the capture/I2S pipeline died (seen after repeated
// reconnects desynced the shared-bus I2S enable state) — the supervisor's
// health watchdog reboots on that, since WakeNet lives in this read path.
static volatile uint32_t s_frame_ticks;

// ---- esp_capture_audio_src_if (mirrors capture_audio_dev_src.c) ----------

static esp_capture_err_t src_open(esp_capture_audio_src_if_t *h)
{
    gated_src_t *s = (gated_src_t *)h;
    if (s->handle == NULL) {
        return ESP_CAPTURE_ERR_NOT_SUPPORTED;
    }
    s->frame_num = 0;
    s->opened = true;
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t src_get_codecs(esp_capture_audio_src_if_t *h,
                                        const esp_capture_format_id_t **codecs,
                                        uint8_t *num)
{
    static esp_capture_format_id_t supported[] = { ESP_CAPTURE_FMT_ID_PCM };
    *codecs = supported;
    *num = 1;
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t src_set_fixed_caps(esp_capture_audio_src_if_t *h,
                                            const esp_capture_audio_info_t *fixed_caps)
{
    if (h == NULL || fixed_caps == NULL) {
        return ESP_CAPTURE_ERR_INVALID_ARG;
    }
    gated_src_t *s = (gated_src_t *)h;
    if (s->started) {
        return ESP_CAPTURE_ERR_INVALID_STATE;
    }
    s->info = *fixed_caps;
    s->use_fixed_caps = (fixed_caps->format_id == ESP_CAPTURE_FMT_ID_PCM);
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t src_negotiate_caps(esp_capture_audio_src_if_t *h,
                                            esp_capture_audio_info_t *in_cap,
                                            esp_capture_audio_info_t *out_caps)
{
    gated_src_t *s = (gated_src_t *)h;
    if (s->use_fixed_caps) {
        if (in_cap->format_id != s->info.format_id) {
            return ESP_CAPTURE_ERR_NOT_SUPPORTED;
        }
        memcpy(out_caps, &s->info, sizeof(esp_capture_audio_info_t));
        return ESP_CAPTURE_ERR_OK;
    }
    const esp_capture_format_id_t *codecs = NULL;
    uint8_t num = 0;
    src_get_codecs(h, &codecs, &num);
    for (int i = 0; i < num; i++) {
        if (codecs[i] == in_cap->format_id) {
            memcpy(out_caps, in_cap, sizeof(esp_capture_audio_info_t));
            s->info = *in_cap;
            return ESP_CAPTURE_ERR_OK;
        }
    }
    return ESP_CAPTURE_ERR_NOT_SUPPORTED;
}

static esp_capture_err_t src_start(esp_capture_audio_src_if_t *h)
{
    gated_src_t *s = (gated_src_t *)h;
    esp_codec_dev_sample_info_t fs = {
        .sample_rate = s->info.sample_rate,
        .bits_per_sample = s->info.bits_per_sample,
        .channel = s->info.channel,
    };
    int ret = esp_codec_dev_open(s->handle, &fs);
    if (ret != 0) {
        ESP_LOGE(TAG, "Failed to open codec device, ret=%d", ret);
        return ESP_CAPTURE_ERR_NOT_SUPPORTED;
    }
    s->started = true;
    s->frame_num = 0;
    s->wn_fill = 0;
    s->fired = false;
    ESP_LOGI(TAG, "capture source started (%s, %dHz/%dch/%dbit)",
             s->wn ? "gated by wake word" : "always awake — no model",
             s->info.sample_rate, s->info.channel, s->info.bits_per_sample);
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t src_read_frame(esp_capture_audio_src_if_t *h,
                                        esp_capture_stream_frame_t *frame)
{
    gated_src_t *s = (gated_src_t *)h;
    if (!s->started) {
        return ESP_CAPTURE_ERR_NOT_SUPPORTED;
    }
    int ret = esp_codec_dev_read(s->handle, frame->data, frame->size);
    if (ret != 0) {
        return ESP_CAPTURE_ERR_INTERNAL;
    }
    int bytes_per_samp = s->info.bits_per_sample / 8 * s->info.channel;
    int samples = bytes_per_samp ? frame->size / bytes_per_samp : 0;

    // ASLEEP: tee the real audio into WakeNet, then blank the frame so only
    // silence is published. Once detection latches (fired), keep publishing
    // silence until the supervisor plays the chime and calls wake_set_awake().
    if (s->wn && !s->awake) {
        const int16_t *pcm = (const int16_t *)frame->data;   // mono 16-bit
        int off = 0;
        while (off < samples) {
            int n = s->wn_chunk - s->wn_fill;
            if (n > samples - off) {
                n = samples - off;
            }
            memcpy(s->wn_accum + s->wn_fill, pcm + off, n * sizeof(int16_t));
            s->wn_fill += n;
            off += n;
            if (s->wn_fill == s->wn_chunk) {
                s->wn_fill = 0;
                if (!s->fired &&
                    s->wn->detect(s->wn_data, s->wn_accum) == WAKENET_DETECTED) {
                    ESP_LOGI(TAG, "wake word detected");
                    s->fired = true;              // stop re-detecting; stay silent
                    if (s->cb) {
                        s->cb(s->cb_ctx);         // supervisor: chime then wake
                    }
                }
            }
        }
        memset(frame->data, 0, frame->size);      // publish silence
    }

    frame->pts = s->frame_num * samples * 1000 /
                 (s->info.sample_rate ? s->info.sample_rate : 16000);
    s->frame_num++;
    s_frame_ticks++;   // liveness for the capture health watchdog
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t src_stop(esp_capture_audio_src_if_t *h)
{
    gated_src_t *s = (gated_src_t *)h;
    if (s->handle) {
        esp_codec_dev_close(s->handle);
    }
    s->started = false;
    return ESP_CAPTURE_ERR_OK;
}

static esp_capture_err_t src_close(esp_capture_audio_src_if_t *h)
{
    gated_src_t *s = (gated_src_t *)h;
    s->opened = false;
    return ESP_CAPTURE_ERR_OK;
}

// ---- public API ----------------------------------------------------------

esp_capture_audio_src_if_t *wake_create_source(esp_codec_dev_handle_t record)
{
    if (record == NULL) {
        return NULL;
    }
    gated_src_t *s = calloc(1, sizeof(gated_src_t));
    if (s == NULL) {
        return NULL;
    }
    s->handle = record;
    s->base.open = src_open;
    s->base.get_support_codecs = src_get_codecs;
    s->base.set_fixed_caps = src_set_fixed_caps;
    s->base.negotiate_caps = src_negotiate_caps;
    s->base.start = src_start;
    s->base.read_frame = src_read_frame;
    s->base.stop = src_stop;
    s->base.close = src_close;

    // Load WakeNet ("Hey Wanda"). If missing, the source runs always-awake
    // (no gating) rather than leaving the device mute.
    srmodel_list_t *models = esp_srmodel_init("model");
    char *name = models ? esp_srmodel_filter(models, ESP_WN_PREFIX, "heywanda")
                        : NULL;
    if (name == NULL && models) {
        name = esp_srmodel_filter(models, ESP_WN_PREFIX, NULL);  // any wake word
    }
    if (name) {
        s->wn = esp_wn_handle_from_name(name);
    }
    if (s->wn) {
        s->wn_data = s->wn->create((const void *)name, DET_MODE_90);
        s->wn_chunk = s->wn->get_samp_chunksize(s->wn_data);
        s->wn_accum = malloc((size_t)s->wn_chunk * sizeof(int16_t));
        if (s->wn_data == NULL || s->wn_accum == NULL) {
            s->wn = NULL;   // fall back to always-awake
        } else {
            ESP_LOGI(TAG, "WakeNet ready: model=%s chunk=%d rate=%dHz", name,
                     s->wn_chunk, s->wn->get_samp_rate(s->wn_data));
        }
    }
    if (s->wn == NULL) {
        ESP_LOGW(TAG, "no wake model — device runs always-awake (no gate)");
        s->awake = true;
    }
    s_src = s;
    return &s->base;
}

void wake_set_detected_cb(wake_detected_cb_t cb, void *ctx)
{
    if (s_src) {
        s_src->cb = cb;
        s_src->cb_ctx = ctx;
    }
}

void wake_set_awake(bool awake)
{
    if (s_src == NULL || s_src->wn == NULL) {
        return;   // no gating without a model
    }
    if (!awake) {
        s_src->fired = false;   // re-arm the detector
        s_src->wn_fill = 0;
    }
    s_src->awake = awake;
}

bool wake_is_gated(void)
{
    return s_src != NULL && s_src->wn != NULL;
}

uint32_t wake_frame_ticks(void)
{
    return s_frame_ticks;
}
