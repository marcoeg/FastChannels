// On-device wake word ("Hey Wanda") via standalone WakeNet.
//
// We use esp-sr's WakeNet iface (esp_wn_*) DIRECTLY on the raw mic — NOT the
// esp_afe_sr front-end. The AFE was removed from this project because
// esp_capture ran its feed/fetch on 4 KB PSRAM stacks, and esp-sr's internal
// cache-disable ops on a PSRAM stack hard-hang the core. Standalone WakeNet
// run from an 8 KB INTERNAL-RAM stack on a pinned core (the pattern Waveshare's
// factory firmware uses on this exact board) sidesteps that entirely.
//
// Mic ownership: the ES7210 record device is single-open (esp_codec_dev
// rejects a second open). While the device is ASLEEP this task owns it; when
// the wake word fires the task closes it and the supervisor hands it to
// esp_capture for publishing. wake_listen_stop() blocks until the mic is
// released so the hand-off is race-free.

#include "wake.h"

#include <string.h>
#include "board.h"
#include "esp_codec_dev.h"
#include "esp_log.h"
#include "esp_wn_iface.h"
#include "esp_wn_models.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "model_path.h"

static const char *TAG = "wake";

static const esp_wn_iface_t *s_wn;
static model_iface_data_t   *s_wn_data;
static int                   s_chunk;        // samples per detect() call
static int16_t              *s_buf;

static wake_detected_cb_t    s_cb;
static void                 *s_cb_ctx;

static TaskHandle_t          s_task;
static SemaphoreHandle_t     s_paused_sem;   // given when mic is released
static volatile bool         s_listening;    // desired: detector owns the mic
static bool                  s_mic_open;      // task-private: mic actually open

static esp_err_t open_mic(void)
{
    esp_codec_dev_sample_info_t fs = {
        .sample_rate = 16000,
        .channel = 1,
        .bits_per_sample = 16,
    };
    int ret = esp_codec_dev_open(get_record_handle(), &fs);
    if (ret != ESP_CODEC_DEV_OK) {
        ESP_LOGE(TAG, "record open failed: %d", ret);
        return ESP_FAIL;
    }
    s_mic_open = true;
    return ESP_OK;
}

static void close_mic(void)
{
    if (s_mic_open) {
        esp_codec_dev_close(get_record_handle());
        s_mic_open = false;
    }
}

static void wakenet_task(void *arg)
{
    for (;;) {
        if (!s_listening) {
            close_mic();
            xSemaphoreGive(s_paused_sem);          // announce mic is free
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY); // block until resume
            continue;
        }
        if (!s_mic_open && open_mic() != ESP_OK) {
            s_listening = false;                    // can't listen without mic
            continue;
        }
        // Read exactly one detection chunk (mono 16-bit). Blocks ~30 ms.
        if (esp_codec_dev_read(get_record_handle(), s_buf,
                               s_chunk * sizeof(int16_t)) != ESP_CODEC_DEV_OK) {
            continue;
        }
        if (s_wn->detect(s_wn_data, s_buf) == WAKENET_DETECTED) {
            ESP_LOGI(TAG, "wake word detected");
            s_listening = false;
            close_mic();                            // release BEFORE notifying
            if (s_cb) {
                s_cb(s_cb_ctx);                     // supervisor -> AWAKE
            }
            // loop: !s_listening -> give sem, block for next resume
        }
    }
}

esp_err_t wake_init(wake_detected_cb_t cb, void *ctx)
{
    s_cb = cb;
    s_cb_ctx = ctx;

    srmodel_list_t *models = esp_srmodel_init("model");
    if (models == NULL) {
        ESP_LOGE(TAG, "no model partition / srmodels.bin");
        return ESP_ERR_NOT_FOUND;
    }
    char *name = esp_srmodel_filter(models, ESP_WN_PREFIX, "heywanda");
    if (name == NULL) {
        name = esp_srmodel_filter(models, ESP_WN_PREFIX, NULL);  // any wakenet
    }
    if (name == NULL) {
        ESP_LOGE(TAG, "no WakeNet model found in partition");
        return ESP_ERR_NOT_FOUND;
    }
    s_wn = esp_wn_handle_from_name(name);
    if (s_wn == NULL) {
        ESP_LOGE(TAG, "no WakeNet handle for %s", name);
        return ESP_ERR_NOT_FOUND;
    }
    s_wn_data = s_wn->create(name, DET_MODE_90);
    s_chunk = s_wn->get_samp_chunksize(s_wn_data);
    ESP_LOGI(TAG, "WakeNet ready: model=%s chunk=%d rate=%d", name, s_chunk,
             s_wn->get_samp_rate(s_wn_data));

    s_buf = malloc(s_chunk * sizeof(int16_t));
    s_paused_sem = xSemaphoreCreateBinary();
    if (s_buf == NULL || s_paused_sem == NULL) {
        return ESP_ERR_NO_MEM;
    }

    // 8 KB INTERNAL-RAM stack, pinned to core 1 (core 0 = WiFi/lwip). Not
    // subscribed to the task WDT: the task legitimately blocks when paused.
    if (xTaskCreatePinnedToCore(wakenet_task, "wakenet", 8 * 1024, NULL, 5,
                                &s_task, 1) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void wake_listen_start(void)
{
    if (s_wn == NULL || s_listening) {
        return;
    }
    s_listening = true;
    xTaskNotifyGive(s_task);   // unblock the paused task
}

void wake_listen_stop(void)
{
    if (s_wn == NULL || !s_listening) {
        return;
    }
    s_listening = false;
    // Wait for the task to finish its current read and close the mic.
    if (xSemaphoreTake(s_paused_sem, pdMS_TO_TICKS(2000)) != pdTRUE) {
        ESP_LOGW(TAG, "timed out waiting for detector to release the mic");
    }
}
