#include "led.h"

#include "esp_log.h"
#include "led_strip.h"

static const char *TAG = "led";

#define LED_GPIO  38
#define LED_COUNT 7

static led_strip_handle_t s_strip;

void led_init(void)
{
    led_strip_config_t strip_cfg = {
        .strip_gpio_num = LED_GPIO,
        .max_leds = LED_COUNT,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_GRB,
    };
    led_strip_rmt_config_t rmt_cfg = {
        .resolution_hz = 10 * 1000 * 1000,
    };
    if (led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &s_strip) != ESP_OK) {
        ESP_LOGE(TAG, "LED strip init failed");
        s_strip = NULL;
        return;
    }
    led_strip_clear(s_strip);
}

static void fill(uint8_t r, uint8_t g, uint8_t b)
{
    if (s_strip == NULL) {
        return;
    }
    for (int i = 0; i < LED_COUNT; i++) {
        led_strip_set_pixel(s_strip, i, r, g, b);
    }
    led_strip_refresh(s_strip);
}

void led_update(ui_conn_t conn, ui_agent_t agent, bool mic_muted)
{
    // Muted trumps everything the user can act on.
    if (conn == UI_CONN_CONNECTED && mic_muted) {
        fill(60, 0, 0);                    // red — mic muted
        return;
    }
    switch (conn) {
        case UI_CONN_BOOT:
        case UI_CONN_WIFI_CONNECTING: fill(12, 12, 12); return;  // dim white
        case UI_CONN_WIFI_FAILED:
        case UI_CONN_FAILED:          fill(60, 4, 0);  return;   // red-orange
        case UI_CONN_CONNECTING:
        case UI_CONN_RECONNECTING:    fill(8, 8, 30);  return;   // dim blue
        default: break;
    }
    switch (agent) {
        case UI_AGENT_LISTENING: fill(0, 55, 18);  break;  // green
        case UI_AGENT_THINKING:  fill(55, 30, 0);  break;  // amber
        case UI_AGENT_SPEAKING:  fill(10, 25, 70); break;  // blue
        case UI_AGENT_NONE:      fill(6, 6, 12);   break;  // faint: no agent
        default:                 fill(3, 10, 10);  break;  // idle teal
    }
}
