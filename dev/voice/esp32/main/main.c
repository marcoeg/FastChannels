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
#include "room.h"
#include "ui.h"

void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_INFO);

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    livekit_system_init();   // must precede any media pipeline setup
    board_init();
    ui_init();
    media_init();

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

    room_start();
}
