// Status screen for the voice satellite: 1.47" touch LCD (JD9853 172x320,
// AXS5106 touch) driven through esp_lvgl_port in landscape (90°, 320x172).
//
// Layout:
//   top bar     "MINNY" + connection status text
//   orb         big colored circle = agent state; tapping it toggles mic
//               mute (touch-to-talk)
//   state text  large label next to the orb
//   bottom bar  speaker mute toggle, volume - / % / +
//
// Display init values (SPI3 @80 MHz, gap (0,34) at 90°, invert on, RGB
// endian, swap_bytes) are taken from Waveshare's lvgl9_3_example_04 for this
// exact board+panel combo. LCD_RST/TP_RST are TCA9555 expander pins.

#include "ui.h"

#include <string.h>
#include "board.h"
#include "led.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_lcd_jd9853.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_touch_axs5106.h"
#include "esp_lvgl_port.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs.h"

static const char *TAG = "ui";

// ---------------------------------------------------------------------------
// Panel geometry / pins (Waveshare ESP32-S3-AUDIO-Board LCD header)
// ---------------------------------------------------------------------------

#define LCD_H_RES        172
#define LCD_V_RES        320
#define LCD_DRAW_BUF_H   24

#define LCD_SPI_HOST     SPI3_HOST
#define LCD_PIXEL_CLK_HZ (80 * 1000 * 1000)
#define LCD_GPIO_SCLK    GPIO_NUM_4
#define LCD_GPIO_MOSI    GPIO_NUM_9
#define LCD_GPIO_CS      GPIO_NUM_3
#define LCD_GPIO_DC      GPIO_NUM_7
#define LCD_GPIO_BL      GPIO_NUM_5

#define EXIO_LCD_RST     IO_EXPANDER_PIN_NUM_0
#define EXIO_TP_RST      IO_EXPANDER_PIN_NUM_1

// ---------------------------------------------------------------------------
// Colors (match the Minny browser client's palette)
// ---------------------------------------------------------------------------

#define COL_BG        0x0b0e14
#define COL_TEXT      0xd8dee9
#define COL_DIM       0x8892a8
#define COL_PANEL     0x1a2233
#define COL_BORDER    0x33415e
#define COL_ORB_OFF   0x2b3950
#define COL_ORB_IDLE  0x33415e
#define COL_LISTEN    0x3fbf7f
#define COL_THINK     0xe0b050
#define COL_SPEAK     0x6f9dff
#define COL_MUTED     0xd05050

// ---------------------------------------------------------------------------
// State model + event queue
// ---------------------------------------------------------------------------

typedef enum { EV_CONN, EV_AGENT, EV_REFRESH } ev_kind_t;
typedef struct {
    ev_kind_t kind;
    int value;
} ui_event_t;

static QueueHandle_t s_queue;
static ui_conn_t  s_conn  = UI_CONN_BOOT;
static ui_agent_t s_agent = UI_AGENT_NONE;
// True when no display module is attached (touch controller absent from
// the I2C bus): status goes to the LED ring only.
static bool s_headless;

static lv_display_t *s_disp;
static lv_obj_t *s_conn_label;
static lv_obj_t *s_orb;
static lv_obj_t *s_state_label;
static lv_obj_t *s_hint_label;
static lv_obj_t *s_spk_label;
static lv_obj_t *s_vol_label;

// ---------------------------------------------------------------------------
// Volume persistence
// ---------------------------------------------------------------------------

static void volume_load(void)
{
    nvs_handle_t nvs;
    if (nvs_open("ui", NVS_READONLY, &nvs) == ESP_OK) {
        uint8_t vol = 0;
        if (nvs_get_u8(nvs, "vol", &vol) == ESP_OK) {
            board_set_volume(vol);
        }
        nvs_close(nvs);
    }
}

static void volume_save(int vol)
{
    nvs_handle_t nvs;
    if (nvs_open("ui", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_set_u8(nvs, "vol", (uint8_t)vol);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
}

// ---------------------------------------------------------------------------
// Rendering (call only from the LVGL task or under lvgl_port_lock)
// ---------------------------------------------------------------------------

static void render(void)
{
    const char *conn_text;
    switch (s_conn) {
        case UI_CONN_BOOT:            conn_text = "starting"; break;
        case UI_CONN_WIFI_CONNECTING: conn_text = "wi-fi"; break;
        case UI_CONN_WIFI_FAILED:     conn_text = "wi-fi failed"; break;
        case UI_CONN_CONNECTING:      conn_text = "connecting"; break;
        case UI_CONN_CONNECTED:       conn_text = "online"; break;
        case UI_CONN_RECONNECTING:    conn_text = "reconnecting"; break;
        default:                      conn_text = "offline"; break;
    }
    lv_label_set_text(s_conn_label, conn_text);

    uint32_t orb = COL_ORB_OFF;
    const char *state_text = "";
    const char *hint_text = "";
    bool muted = board_get_mic_muted();

    if (s_conn != UI_CONN_CONNECTED) {
        switch (s_conn) {
            case UI_CONN_BOOT:            state_text = "starting..."; break;
            case UI_CONN_WIFI_CONNECTING: state_text = "joining Wi-Fi..."; break;
            case UI_CONN_WIFI_FAILED:     state_text = "Wi-Fi failed"; orb = COL_MUTED; break;
            case UI_CONN_CONNECTING:      state_text = "connecting..."; break;
            case UI_CONN_RECONNECTING:    state_text = "reconnecting..."; break;
            default:                      state_text = "disconnected"; orb = COL_MUTED; break;
        }
        hint_text = "";
    } else if (muted) {
        orb = COL_MUTED;
        state_text = "mic muted";
        hint_text = "tap the circle to unmute";
    } else {
        switch (s_agent) {
            case UI_AGENT_NONE:
                orb = COL_ORB_OFF;
                state_text = "waiting for Minny";
                hint_text = "";
                break;
            case UI_AGENT_INITIALIZING:
                orb = COL_ORB_IDLE;
                state_text = "Minny is waking up";
                hint_text = "";
                break;
            case UI_AGENT_LISTENING:
                orb = COL_LISTEN;
                state_text = "listening";
                hint_text = "just talk";
                break;
            case UI_AGENT_THINKING:
                orb = COL_THINK;
                state_text = "thinking...";
                hint_text = "";
                break;
            case UI_AGENT_SPEAKING:
                orb = COL_SPEAK;
                state_text = "speaking";
                hint_text = "talk to interrupt";
                break;
            case UI_AGENT_IDLE:
            default:
                orb = COL_ORB_IDLE;
                state_text = "ready";
                hint_text = "just talk";
                break;
        }
    }

    // No drop shadow on purpose: LVGL's software shadow blur of the big
    // circle costs more per redraw than everything else on screen combined
    // (it pegged a core). Active states get a bright border instead.
    lv_obj_set_style_bg_color(s_orb, lv_color_hex(orb), 0);
    bool active = (orb == COL_LISTEN || orb == COL_SPEAK || orb == COL_THINK);
    lv_obj_set_style_border_color(s_orb,
        lv_color_hex(active ? orb : COL_BORDER), 0);
    lv_obj_set_style_border_width(s_orb, active ? 4 : 2, 0);
    lv_label_set_text(s_state_label, state_text);
    lv_label_set_text(s_hint_label, hint_text);

    lv_label_set_text_fmt(s_vol_label, "%d%%", board_get_volume());
}

// ---------------------------------------------------------------------------
// LVGL event callbacks (run inside the LVGL task — no locking needed)
// ---------------------------------------------------------------------------

static void orb_clicked(lv_event_t *e)
{
    board_set_mic_muted(!board_get_mic_muted());
    render();
}

static void spk_clicked(lv_event_t *e)
{
    static bool spk_muted = false;
    spk_muted = !spk_muted;
    board_set_speaker_enabled(!spk_muted);
    lv_label_set_text(s_spk_label, spk_muted ? LV_SYMBOL_MUTE : LV_SYMBOL_VOLUME_MAX);
    lv_obj_set_style_text_color(s_spk_label,
        lv_color_hex(spk_muted ? COL_MUTED : COL_TEXT), 0);
}

static void vol_clicked(lv_event_t *e)
{
    int delta = (int)(intptr_t)lv_event_get_user_data(e);
    int vol = board_get_volume() + delta;
    board_set_volume(vol);
    volume_save(board_get_volume());
    render();
}

// ---------------------------------------------------------------------------
// Widgets
// ---------------------------------------------------------------------------

static lv_obj_t *make_button(lv_obj_t *parent, int x, int y, int w, int h,
                             const char *text, lv_event_cb_t cb, void *user_data,
                             lv_obj_t **out_label)
{
    lv_obj_t *btn = lv_button_create(parent);
    lv_obj_set_pos(btn, x, y);
    lv_obj_set_size(btn, w, h);
    lv_obj_set_style_bg_color(btn, lv_color_hex(COL_PANEL), 0);
    lv_obj_set_style_border_color(btn, lv_color_hex(COL_BORDER), 0);
    lv_obj_set_style_border_width(btn, 1, 0);
    lv_obj_set_style_radius(btn, 8, 0);
    lv_obj_set_style_shadow_width(btn, 0, 0);
    lv_obj_add_event_cb(btn, cb, LV_EVENT_CLICKED, user_data);

    lv_obj_t *label = lv_label_create(btn);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_color(label, lv_color_hex(COL_TEXT), 0);
    lv_obj_center(label);
    if (out_label) {
        *out_label = label;
    }
    return btn;
}

// Temporary bring-up diagnostic: catches phantom touch presses (a
// misbehaving controller would spam this and constantly invalidate the UI).
static void dbg_pressed(lv_event_t *e)
{
    lv_indev_t *indev = lv_indev_active();
    if (indev) {
        lv_point_t p;
        lv_indev_get_point(indev, &p);
        ESP_LOGI(TAG, "touch press at %d,%d", (int)p.x, (int)p.y);
    }
}

static void build_screen(void)
{
    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(COL_BG), 0);
    lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(scr, dbg_pressed, LV_EVENT_PRESSED, NULL);

    // Top bar
    lv_obj_t *title = lv_label_create(scr);
    lv_label_set_text(title, "MINNY");
    lv_obj_set_style_text_color(title, lv_color_hex(COL_TEXT), 0);
    lv_obj_set_style_text_letter_space(title, 2, 0);
    lv_obj_set_pos(title, 10, 8);

    s_conn_label = lv_label_create(scr);
    lv_label_set_text(s_conn_label, "starting");
    lv_obj_set_style_text_color(s_conn_label, lv_color_hex(COL_DIM), 0);
    lv_obj_align(s_conn_label, LV_ALIGN_TOP_RIGHT, -10, 8);

    // Orb (touch-to-talk / mic mute toggle)
    s_orb = lv_obj_create(scr);
    lv_obj_set_size(s_orb, 84, 84);
    lv_obj_set_pos(s_orb, 22, 44);
    lv_obj_set_style_radius(s_orb, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(s_orb, lv_color_hex(COL_ORB_OFF), 0);
    lv_obj_set_style_border_color(s_orb, lv_color_hex(COL_BORDER), 0);
    lv_obj_set_style_border_width(s_orb, 2, 0);
    lv_obj_set_style_shadow_width(s_orb, 0, 0);
    lv_obj_remove_flag(s_orb, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_orb, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_orb, orb_clicked, LV_EVENT_CLICKED, NULL);

    // State + hint labels
    s_state_label = lv_label_create(scr);
    lv_label_set_text(s_state_label, "starting...");
    lv_obj_set_style_text_color(s_state_label, lv_color_hex(COL_TEXT), 0);
#if LV_FONT_MONTSERRAT_20
    lv_obj_set_style_text_font(s_state_label, &lv_font_montserrat_20, 0);
#endif
    lv_obj_set_pos(s_state_label, 124, 64);
    lv_obj_set_width(s_state_label, 188);
    lv_label_set_long_mode(s_state_label, LV_LABEL_LONG_WRAP);

    s_hint_label = lv_label_create(scr);
    lv_label_set_text(s_hint_label, "");
    lv_obj_set_style_text_color(s_hint_label, lv_color_hex(COL_DIM), 0);
    lv_obj_set_pos(s_hint_label, 124, 96);
    lv_obj_set_width(s_hint_label, 188);

    // Bottom bar: speaker mute, volume - / % / +
    make_button(scr, 10, 132, 56, 32, LV_SYMBOL_VOLUME_MAX, spk_clicked, NULL, &s_spk_label);
    make_button(scr, 188, 132, 36, 32, LV_SYMBOL_MINUS, vol_clicked, (void *)(intptr_t)-10, NULL);

    s_vol_label = lv_label_create(scr);
    lv_label_set_text_fmt(s_vol_label, "%d%%", board_get_volume());
    lv_obj_set_style_text_color(s_vol_label, lv_color_hex(COL_TEXT), 0);
    lv_obj_set_pos(s_vol_label, 232, 140);

    make_button(scr, 274, 132, 36, 32, LV_SYMBOL_PLUS, vol_clicked, (void *)(intptr_t)10, NULL);

    render();
}

// ---------------------------------------------------------------------------
// Display / touch bring-up
// ---------------------------------------------------------------------------

// Bring-up: plain GPIO drive instead of LEDC PWM — removes any PWM-config
// doubt while diagnosing a dark panel. Restore the demo's LEDC dimming
// (13-bit @5 kHz, duty 8191-81*(100-x)) once the display is proven.
static void backlight_init(void)
{
    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << LCD_GPIO_BL,
        .mode = GPIO_MODE_OUTPUT,
    };
    ESP_ERROR_CHECK(gpio_config(&cfg));
    gpio_set_level(LCD_GPIO_BL, 1);   // on immediately, full brightness
}

static void backlight_set(int percent)
{
    gpio_set_level(LCD_GPIO_BL, percent > 0 ? 1 : 0);
}

static void expander_pulse(esp_io_expander_pin_num_t pin, int low_ms, int high_ms)
{
    esp_io_expander_handle_t exp = board_get_io_expander();
    esp_io_expander_set_level(exp, pin, 0);
    vTaskDelay(pdMS_TO_TICKS(low_ms));
    esp_io_expander_set_level(exp, pin, 1);
    vTaskDelay(pdMS_TO_TICKS(high_ms));
}

static void display_init(void)
{
    // Reset the panel via the expander (no GPIO reset line on this board).
    expander_pulse(EXIO_LCD_RST, 10, 50);

    const spi_bus_config_t bus_cfg = {
        .sclk_io_num = LCD_GPIO_SCLK,
        .mosi_io_num = LCD_GPIO_MOSI,
        .miso_io_num = GPIO_NUM_8,    // LCD_MISO — used only for diagnostics
        .quadwp_io_num = GPIO_NUM_NC,
        .quadhd_io_num = GPIO_NUM_NC,
        // Landscape flush lines are V_RES wide.
        .max_transfer_sz = LCD_V_RES * LCD_DRAW_BUF_H * sizeof(uint16_t),
    };
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_SPI_HOST, &bus_cfg, SPI_DMA_CH_AUTO));

    const esp_lcd_panel_io_spi_config_t io_cfg = {
        .dc_gpio_num = LCD_GPIO_DC,
        .cs_gpio_num = LCD_GPIO_CS,
        .pclk_hz = LCD_PIXEL_CLK_HZ,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .spi_mode = 0,
        .trans_queue_depth = 10,
    };
    esp_lcd_panel_io_handle_t io_handle;
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(
        (esp_lcd_spi_bus_handle_t)LCD_SPI_HOST, &io_cfg, &io_handle));

    const esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num = GPIO_NUM_NC,       // reset handled via EXIO0 above
        .rgb_endian = LCD_RGB_ENDIAN_RGB,
        .bits_per_pixel = 16,
    };
    esp_lcd_panel_handle_t panel;
    ESP_ERROR_CHECK(esp_lcd_new_panel_jd9853(io_handle, &panel_cfg, &panel));

    // 90° landscape: the 34-px GRAM window offset moves to the y axis.
    ESP_ERROR_CHECK(esp_lcd_panel_set_gap(panel, 0, 34));
    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel));
    ESP_ERROR_CHECK(esp_lcd_panel_invert_color(panel, true));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel, true));

    // Bring-up diagnostic: read the controller's ID (RDDID 0x04) and status
    // (RDDST 0x09) back over MISO. A sane, non-00/FF answer proves the SPI
    // link and panel logic are alive (=> dark screen is backlight power);
    // all-zeros/FF means the ribbon's data contacts aren't landing.
    {
        uint8_t id[4] = {0}, st[5] = {0};
        esp_err_t r1 = esp_lcd_panel_io_rx_param(io_handle, 0x04, id, sizeof(id));
        esp_err_t r2 = esp_lcd_panel_io_rx_param(io_handle, 0x09, st, sizeof(st));
        ESP_LOGI(TAG, "panel RDDID(%s): %02X %02X %02X %02X | RDDST(%s): %02X %02X %02X %02X %02X",
                 esp_err_to_name(r1), id[0], id[1], id[2], id[3],
                 esp_err_to_name(r2), st[0], st[1], st[2], st[3], st[4]);
    }

    // Core 1: the AFE/AEC + WiFi + lwip saturate core 0 (measured — IDLE0
    // watchdog), while core 1 (Opus encode, peer tasks) has headroom. LVGL
    // at priority 4 yields to all media tasks there.
    const lvgl_port_cfg_t port_cfg = {
        .task_priority = 4,
        .task_stack = 8192,
        .task_affinity = 1,
        .task_max_sleep_ms = 500,
        .timer_period_ms = 20,
    };
    ESP_ERROR_CHECK(lvgl_port_init(&port_cfg));

    const lvgl_port_display_cfg_t disp_cfg = {
        .io_handle = io_handle,
        .panel_handle = panel,
        // Single small draw buffer in INTERNAL DMA RAM: a PSRAM buffer
        // forces spi_master to allocate a transfer-sized internal bounce
        // buffer on every flush, which failed (and wedged LVGL) once
        // WiFi + the LiveKit engine had claimed internal heap.
        .buffer_size = LCD_V_RES * LCD_DRAW_BUF_H,
        .double_buffer = false,
        .hres = LCD_V_RES,            // 320 (landscape)
        .vres = LCD_H_RES,            // 172
        .monochrome = false,
        .color_format = LV_COLOR_FORMAT_RGB565,
        .rotation = {                 // 90°
            .swap_xy = true,
            .mirror_x = true,
            .mirror_y = false,
        },
        .flags = {
            .buff_dma = true,
            .buff_spiram = false,
            .swap_bytes = true,
        },
    };
    s_disp = lvgl_port_add_disp(&disp_cfg);

    // Touch: AXS5106 on the shared I2C bus; reset via EXIO1 is mandatory.
    expander_pulse(EXIO_TP_RST, 50, 50);

    i2c_device_config_t touch_dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = ESP_LCD_TOUCH_IO_I2C_AXS5106_ADDRESS,
        .scl_speed_hz = 400000,
    };
    i2c_master_dev_handle_t touch_dev;
    ESP_ERROR_CHECK(i2c_master_bus_add_device(board_get_i2c_bus(), &touch_dev_cfg, &touch_dev));

    // 90° values from the demo's AXS5106-specific branch (touch_driver.c:84):
    // limits are post-swap (320x172) and no mirror — esp_lcd_touch applies
    // mirrors BEFORE swap, so the generic CST816-style {mirror_x=1} branch
    // flips the Y axis on this controller.
    esp_lcd_touch_config_t tp_cfg = {
        .x_max = LCD_V_RES,
        .y_max = LCD_H_RES,
        .rst_gpio_num = GPIO_NUM_NC,
        .int_gpio_num = GPIO_NUM_NC,  // TP_INT is on the expander — polled
        .flags = {
            .swap_xy = 1,
            .mirror_x = 0,
            .mirror_y = 0,
        },
    };
    esp_lcd_touch_handle_t tp;
    ESP_ERROR_CHECK(esp_lcd_touch_new_i2c_axs5106(touch_dev, &tp_cfg, &tp));

    const lvgl_port_touch_cfg_t touch_cfg = {
        .disp = s_disp,
        .handle = tp,
    };
    lvgl_port_add_touch(&touch_cfg);
}

// ---------------------------------------------------------------------------
// Event task
// ---------------------------------------------------------------------------

static void ui_task(void *arg)
{
    ui_event_t ev;
    for (;;) {
        // 1 s timeout = periodic repaint: WS2812 frames occasionally get
        // corrupted (observed: a green write reported OK, ring stayed on
        // the old color), and event-only painting freezes a glitched frame
        // forever. The repaint self-heals within a second.
        bool got_event = xQueueReceive(s_queue, &ev, pdMS_TO_TICKS(1000)) == pdTRUE;
        // Leak watch: the peer connection has died every ~2-3 minutes in
        // long sessions — track internal heap to catch exhaustion.
        static int tick;
        if (++tick >= 30) {
            tick = 0;
            ESP_LOGI(TAG, "heap watch: internal=%u dma=%u",
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_DMA));
        }
        if (got_event) {
            switch (ev.kind) {
                case EV_CONN:  s_conn = (ui_conn_t)ev.value; break;
                case EV_AGENT: s_agent = (ui_agent_t)ev.value; break;
                default: break;
            }
        }
        led_update(s_conn, s_agent, board_get_mic_muted());
        if (!got_event) {
            continue;
        }
        if (!s_headless && lvgl_port_lock(0)) {   // 0 = block until acquired
            render();
            lvgl_port_unlock();
        }
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void ui_init(void)
{
    volume_load();
    led_init();
    led_self_test();
    led_update(UI_CONN_BOOT, UI_AGENT_NONE, false);
    s_queue = xQueueCreate(16, sizeof(ui_event_t));
    xTaskCreatePinnedToCore(ui_task, "ui_events", 4096, NULL, 4, NULL, 1);

    // Display auto-detection: the touch controller shares the module's FPC,
    // so its I2C presence is the "display attached" signal. Without it,
    // run headless on the LED ring (touch init would otherwise abort).
    if (i2c_master_probe(board_get_i2c_bus(),
                         ESP_LCD_TOUCH_IO_I2C_AXS5106_ADDRESS, 100) != ESP_OK) {
        s_headless = true;
        ESP_LOGW(TAG, "no display module detected — running headless (LED ring only)");
        return;
    }

    backlight_init();
    display_init();

    // Bring-up diagnostic: flood the panel white for 1.5 s before drawing
    // the UI. If the screen stays dark through this, the problem is
    // backlight power or the panel itself — not rendering.
    if (lvgl_port_lock(0)) {
        lv_obj_set_style_bg_color(lv_screen_active(), lv_color_hex(0xFFFFFF), 0);
        lvgl_port_unlock();
    }
    ESP_LOGI(TAG, "screen should now be WHITE");
    vTaskDelay(pdMS_TO_TICKS(1500));

    if (lvgl_port_lock(0)) {
        build_screen();
        lvgl_port_unlock();
    }
    ESP_LOGI(TAG, "UI ready (320x172 landscape)");
}

static void post(ev_kind_t kind, int value)
{
    if (s_queue == NULL) {
        return;
    }
    ui_event_t ev = { .kind = kind, .value = value };
    xQueueSend(s_queue, &ev, 0);
}

void ui_post_conn(ui_conn_t state)   { post(EV_CONN, state); }
void ui_post_agent(ui_agent_t state) { post(EV_AGENT, state); }
