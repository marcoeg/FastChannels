#include "led.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "led_strip.h"

static void fill(uint8_t r, uint8_t g, uint8_t b);

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
        // This board's ring is wired RGB order, not the usual WS2812 GRB.
        // Verified empirically: with GRB, listening(0,60,0) showed red,
        // thinking(55,30,0) showed green, speaking(10,25,70) showed blue —
        // exactly a red/green byte swap. RGB makes all three correct.
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_RGB,
    };
    // RMT backend — the configuration that showed correct colors in the
    // first headless build. (An SPI-backend experiment showed the same
    // wrong-color symptom, so the backend was not the culprit.)
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

// Boot self-test: pure red, green, blue for 1.5 s each. The observed order
// pins down any color-order/timing remap between driver and LEDs.
void led_self_test(void)
{
    ESP_LOGI(TAG, "self-test: RED");
    fill(60, 0, 0);
    vTaskDelay(pdMS_TO_TICKS(1500));
    ESP_LOGI(TAG, "self-test: GREEN");
    fill(0, 60, 0);
    vTaskDelay(pdMS_TO_TICKS(1500));
    ESP_LOGI(TAG, "self-test: BLUE");
    fill(0, 0, 60);
    vTaskDelay(pdMS_TO_TICKS(1500));
}

static void fill(uint8_t r, uint8_t g, uint8_t b)
{
    static uint8_t last_r = 255, last_g = 255, last_b = 255;
    if (s_strip == NULL) {
        return;
    }
    for (int i = 0; i < LED_COUNT; i++) {
        led_strip_set_pixel(s_strip, i, r, g, b);
    }
    esp_err_t err = led_strip_refresh(s_strip);
    // Log only actual color changes; the 1 Hz repaint stays quiet.
    if (r != last_r || g != last_g || b != last_b) {
        ESP_LOGI(TAG, "ring <- %u,%u,%u (%s)", r, g, b, esp_err_to_name(err));
        last_r = r; last_g = g; last_b = b;
    }
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
        case UI_AGENT_LISTENING: fill(0, 60, 0);   break;  // pure green (the
            // blue-tinted 0,55,18 read as violet on the ring when idle)
        case UI_AGENT_THINKING:  fill(55, 30, 0);  break;  // amber
        case UI_AGENT_SPEAKING:  fill(10, 25, 70); break;  // blue
        case UI_AGENT_NONE:      fill(20, 12, 0);  break;  // dim amber: no agent yet
        default:                 fill(0, 20, 6);   break;  // dim green: idle/ready
    }
}
