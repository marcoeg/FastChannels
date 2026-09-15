// Minny voice satellite — Waveshare ESP32-S3-AUDIO-Board + 1.47" Touch LCD.
//
// Joins the self-hosted LiveKit room as a microphone/speaker/status-screen
// participant; the Minny agent (dev/voice/voice_agent.py) does the talking.

#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "livekit.h"
#include "livekit_example_utils.h"
#include "nvs_flash.h"

#include "board.h"
#include "media.h"
#include "net_log.h"
#include "room.h"
#include "ui.h"

// Internal RAM is the scarce resource here (FreeRTOS objects, task stacks
// and SPI DMA must all live there) — log it at each stage so regressions
// show up in the boot log instead of as mystery init failures.
static void log_heap(const char *stage)
{
    ESP_LOGI("heap", "%s: internal=%u dma=%u total=%u", stage,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_DMA),
             (unsigned)esp_get_free_heap_size());
}

void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_INFO);
    // CRITICAL: with no host reading the USB-Serial-JTAG console, log
    // writes stall the printing task — and esp_peer prints hundreds of
    // benign "Not found for bind remote" errors inside the ICE hot path.
    // Unwatched boots wedged at CONNECTING forever; boots with a serial
    // monitor attached always connected. Filter the flood before it prints.
    esp_log_level_set("AGENT", ESP_LOG_NONE);
    esp_log_level_set("livekit_protocol", ESP_LOG_NONE);

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    livekit_system_init();   // must precede any media pipeline setup
    board_init();
    board_play_test_tone();  // bring-up: audible = speaker path good
    log_heap("after board_init");
    ui_init();
    log_heap("after ui_init");
    media_init();
    log_heap("after media_init");

    // SNTP: JWT nbf/exp are validated server-side; a sane clock avoids
    // spurious auth failures after long power-offs.
    esp_sntp_config_t sntp_config = ESP_NETIF_SNTP_DEFAULT_CONFIG_MULTIPLE(
        2, ESP_SNTP_SERVER_LIST("time.google.com", "pool.ntp.org"));
    esp_netif_sntp_init(&sntp_config);

    ui_post_conn(UI_CONN_WIFI_CONNECTING);
    if (!lk_example_network_connect()) {
        ui_post_conn(UI_CONN_WIFI_FAILED);
        ESP_LOGE("main", "Network connection failed — check WiFi credentials");
        return;
    }
    log_heap("after network");
    net_log_start("192.168.50.17", 5514);
    log_heap("after net_log");

    room_start();
    log_heap("after room_start");
}
