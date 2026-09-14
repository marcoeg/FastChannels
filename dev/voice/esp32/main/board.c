// Board initialization for the Waveshare ESP32-S3-AUDIO-Board.
//
// Adapted from the LiveKit SDK custom_hardware example (which targets the
// Waveshare ESP32-S3-Touch-LCD-1.83 with the same ES8311+ES7210 codec pair).
// Differences on this board, all confirmed against schematic v1.1 and
// Waveshare's own demo package:
//   - I2C0 on GPIO10 (SCL) / GPIO11 (SDA), shared with TCA9555 expander,
//     PCF85063 RTC and the LCD touch controller (AXS5106 @0x63).
//   - I2S1 on MCLK=12 BCLK=13 WS=14 DIN=15 (from ES7210) DOUT=16 (to ES8311).
//   - No AXP2101 PMU; rails are fixed 3.3 V from USB/battery.
//   - The NS4150B speaker amp enable is TCA9555 EXIO8 (active high), not an
//     ESP GPIO, so es8311's pa_pin stays -1 and we drive the expander.
//   - AEC reference is wired in hardware: ES8311 OUT± -> attenuator ->
//     ES7210 MIC3. On the wire the ES7210 TDM slot order is
//     slot0=MIC1 (mic), slot1=MIC3 (reference), slot2=MIC2, slot3=MIC4(n/c),
//     which is what makes media.c's channel_mask=slots 0+1 / "MR" layout
//     pick up mic + echo reference. (Waveshare's demo reads the same frames
//     through 32-bit stereo slots and calls the byte-swapped view "RMNM".)

#include "board.h"

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/i2s_tdm.h"
#include "esp_check.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_io_expander_tca95xx_16bit.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "board";

// ---------------------------------------------------------------------------
// Pin definitions — Waveshare ESP32-S3-AUDIO-Board (schematic v1.1)
// ---------------------------------------------------------------------------

#define BOARD_I2C_SDA  GPIO_NUM_11
#define BOARD_I2C_SCL  GPIO_NUM_10

#define BOARD_I2S_PORT I2S_NUM_1
#define BOARD_I2S_MCLK GPIO_NUM_12
#define BOARD_I2S_BCLK GPIO_NUM_13
#define BOARD_I2S_WS   GPIO_NUM_14
#define BOARD_I2S_DOUT GPIO_NUM_16  // ESP32 -> ES8311 (playback)
#define BOARD_I2S_DIN  GPIO_NUM_15  // ES7210 -> ESP32 (recording)

// Codec I2C addresses — 8-bit format as required by esp_codec_dev.
//   ES8311: CE low  -> 7-bit 0x18, 8-bit 0x30
//   ES7210: AD0=AD1=0 -> 7-bit 0x40, 8-bit 0x80
#define ES8311_ADDR    ES8311_CODEC_DEFAULT_ADDR   // 0x30
#define ES7210_ADDR    ES7210_CODEC_DEFAULT_ADDR   // 0x80

// TCA9555 expander pins
#define EXIO_LCD_RST   IO_EXPANDER_PIN_NUM_0
#define EXIO_TP_RST    IO_EXPANDER_PIN_NUM_1
#define EXIO_TP_INT    IO_EXPANDER_PIN_NUM_2
#define EXIO_PA_EN     IO_EXPANDER_PIN_NUM_8
#define EXIO_KEY1      IO_EXPANDER_PIN_NUM_9
#define EXIO_KEY2      IO_EXPANDER_PIN_NUM_10
#define EXIO_KEY3      IO_EXPANDER_PIN_NUM_11

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

static i2c_master_bus_handle_t  i2c_bus;
static esp_io_expander_handle_t io_expander;
static i2s_chan_handle_t        i2s_tx;
static i2s_chan_handle_t        i2s_rx;
// ONE data interface shared by both codec devices: all of esp_codec_dev's
// full-duplex bookkeeping (slot-width extension, "reconfig TX when RX
// changes" fixup) is per-instance, so separate instances — as in both
// vendor examples — let a playback open shrink the shared BCLK under a
// running capture (or vice versa).
static const audio_codec_data_if_t *i2s_data_if;
static esp_codec_dev_handle_t   play_dev;
static esp_codec_dev_handle_t   rec_dev;
static int  cur_volume = CONFIG_SPEAKER_VOLUME;
static bool mic_muted  = false;

// ---------------------------------------------------------------------------
// Step 1: I2C
// ---------------------------------------------------------------------------

static esp_err_t init_i2c(void)
{
    i2c_master_bus_config_t cfg = {
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .i2c_port   = I2C_NUM_0,
        .scl_io_num = BOARD_I2C_SCL,
        .sda_io_num = BOARD_I2C_SDA,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,   // board has 2.2k externals too
    };
    return i2c_new_master_bus(&cfg, &i2c_bus);
}

// Diagnostic probe of the bus. Expected ACKs on this board:
// 0x18 (ES8311), 0x20 (TCA9555), 0x40 (ES7210), 0x51 (PCF85063 RTC),
// 0x63 (AXS5106 touch, only after its reset line is released).
static void i2c_bus_scan(void)
{
    ESP_LOGI(TAG, "Scanning I2C bus ...");
    int found = 0;
    for (uint8_t addr = 0x03; addr <= 0x77; addr++) {
        if (i2c_master_probe(i2c_bus, addr, 50) == ESP_OK) {
            ESP_LOGI(TAG, "  0x%02X — ACK", addr);
            found++;
        }
    }
    ESP_LOGI(TAG, "I2C scan complete — %d device(s) found", found);
}

// ---------------------------------------------------------------------------
// Step 2: TCA9555 IO expander (must precede LCD/touch init and PA control)
// ---------------------------------------------------------------------------

static esp_err_t init_io_expander(void)
{
    ESP_RETURN_ON_ERROR(
        esp_io_expander_new_i2c_tca95xx_16bit(
            i2c_bus, ESP_IO_EXPANDER_I2C_TCA9555_ADDRESS_000, &io_expander),
        TAG, "TCA9555 init failed");

    esp_io_expander_set_dir(io_expander,
        EXIO_LCD_RST | EXIO_TP_RST | EXIO_PA_EN, IO_EXPANDER_OUTPUT);
    esp_io_expander_set_dir(io_expander,
        EXIO_TP_INT | EXIO_KEY1 | EXIO_KEY2 | EXIO_KEY3, IO_EXPANDER_INPUT);

    // PA off until the codecs are up; LCD/touch held out of reset.
    esp_io_expander_set_level(io_expander, EXIO_PA_EN, 0);
    esp_io_expander_set_level(io_expander, EXIO_LCD_RST, 1);
    esp_io_expander_set_level(io_expander, EXIO_TP_RST, 1);
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// Step 3: I2S — one full-duplex port shared by both codecs.
//
// Both channels are initialized with the IDENTICAL TDM config on purpose:
// the IDF driver only constitutes a real full-duplex pair (shared
// BCLK/WS/MCLK, one clock source) when the two channels' mode configs match
// exactly (s_i2s_channel_try_to_constitude_tdm_duplex in i2s_tdm.c). The
// LiveKit custom_hardware example's TX-std + RX-TDM split never pairs, so
// two independent master clocks fight over the shared pins.
//
// TDM (not std) is also what lets the record path use hardware slot
// masking: esp_codec_dev reconfigures slots on open (playback ends up
// 2x32-bit Philips = plain I2S for the ES8311; record 4x16-bit with the
// hardware mask selecting slots 0+1 = mic + AEC reference) and both
// directions stay at 64 bits/frame -> one coherent 1.024 MHz BCLK.
// ---------------------------------------------------------------------------

static esp_err_t init_i2s(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(BOARD_I2S_PORT, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, &i2s_tx, &i2s_rx),
                        TAG, "Failed to create I2S channels");

    i2s_tdm_slot_mask_t slot_mask =
        I2S_TDM_SLOT0 | I2S_TDM_SLOT1 | I2S_TDM_SLOT2 | I2S_TDM_SLOT3;
    i2s_tdm_config_t tdm_cfg = {
        .clk_cfg  = I2S_TDM_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_TDM_PHILIPS_SLOT_DEFAULT_CONFIG(32, I2S_SLOT_MODE_STEREO, slot_mask),
        .gpio_cfg = {
            .mclk = BOARD_I2S_MCLK,
            .bclk = BOARD_I2S_BCLK,
            .ws   = BOARD_I2S_WS,
            .dout = BOARD_I2S_DOUT,
            .din  = BOARD_I2S_DIN,
        },
    };
    tdm_cfg.slot_cfg.total_slot = 4;
    ESP_RETURN_ON_ERROR(i2s_channel_init_tdm_mode(i2s_tx, &tdm_cfg),
                        TAG, "Failed to init I2S TX (TDM)");
    ESP_RETURN_ON_ERROR(i2s_channel_init_tdm_mode(i2s_rx, &tdm_cfg),
                        TAG, "Failed to init I2S RX (TDM)");

    // Some codecs need the clock running during register configuration.
    i2s_channel_enable(i2s_tx);
    i2s_channel_enable(i2s_rx);
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// Step 4: ES8311 (DAC — speaker output)
// ---------------------------------------------------------------------------

static esp_err_t init_es8311(void)
{
    audio_codec_i2c_cfg_t i2c_cfg = {
        .port       = I2C_NUM_0,
        .bus_handle = i2c_bus,
        .addr       = ES8311_ADDR,
    };
    const audio_codec_ctrl_if_t *ctrl = audio_codec_new_i2c_ctrl(&i2c_cfg);
    ESP_RETURN_ON_FALSE(ctrl, ESP_FAIL, TAG, "ES8311 I2C ctrl failed");

    const audio_codec_gpio_if_t *gpio = audio_codec_new_gpio();
    ESP_RETURN_ON_FALSE(gpio, ESP_FAIL, TAG, "GPIO interface failed");

    es8311_codec_cfg_t codec_cfg = {
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .ctrl_if    = ctrl,
        .gpio_if    = gpio,
        .pa_pin     = -1,           // PA is on the TCA9555 (EXIO8), not a GPIO
        // Clock from MCLK: the capture side needs 4x16-bit TDM frames (the
        // electrical AEC reference only exists in the full 4-slot stream),
        // which puts 16-bit data in 32-bit playback slots — a geometry the
        // BCLK-derived clocking gets wrong (silent DAC, observed live).
        // MCLK stays at 256*fs regardless of slot width.
        .use_mclk   = true,
    };
    const audio_codec_if_t *codec = es8311_codec_new(&codec_cfg);
    ESP_RETURN_ON_FALSE(codec, ESP_FAIL, TAG, "ES8311 codec init failed");

    esp_codec_dev_cfg_t dev_cfg = {
        .codec_if = codec,
        .data_if  = i2s_data_if,
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
    };
    play_dev = esp_codec_dev_new(&dev_cfg);
    ESP_RETURN_ON_FALSE(play_dev, ESP_FAIL, TAG, "Playback device creation failed");

    esp_codec_dev_set_out_vol(play_dev, cur_volume);
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// Step 5: ES7210 (ADC — microphones + hardware AEC reference on MIC3)
// ---------------------------------------------------------------------------

static esp_err_t init_es7210(void)
{
    audio_codec_i2c_cfg_t i2c_cfg = {
        .port       = I2C_NUM_0,
        .bus_handle = i2c_bus,
        .addr       = ES7210_ADDR,
    };
    const audio_codec_ctrl_if_t *ctrl = audio_codec_new_i2c_ctrl(&i2c_cfg);
    ESP_RETURN_ON_FALSE(ctrl, ESP_FAIL, TAG, "ES7210 I2C ctrl failed");

    // All four inputs selected puts the chip in TDM mode (mics on MIC1/MIC2,
    // DAC loopback on MIC3, MIC4 unconnected) — same as Waveshare's demos.
    es7210_codec_cfg_t codec_cfg = {
        .ctrl_if      = ctrl,
        .mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2 |
                        ES7210_SEL_MIC3 | ES7210_SEL_MIC4,
    };
    const audio_codec_if_t *codec = es7210_codec_new(&codec_cfg);
    ESP_RETURN_ON_FALSE(codec, ESP_FAIL, TAG, "ES7210 codec init failed");

    esp_codec_dev_cfg_t dev_cfg = {
        .codec_if = codec,
        .data_if  = i2s_data_if,
        .dev_type = ESP_CODEC_DEV_TYPE_IN,
    };
    rec_dev = esp_codec_dev_new(&dev_cfg);
    ESP_RETURN_ON_FALSE(rec_dev, ESP_FAIL, TAG, "Record device creation failed");

    esp_codec_dev_set_in_gain(rec_dev, CONFIG_MIC_GAIN_DB);
    return ESP_OK;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void board_init(void)
{
    ESP_LOGI(TAG, "Initializing Waveshare ESP32-S3-AUDIO-Board");

    ESP_ERROR_CHECK(init_i2c());
    ESP_ERROR_CHECK(init_io_expander());
    i2c_bus_scan();
    ESP_ERROR_CHECK(init_i2s());

    audio_codec_i2s_cfg_t data_cfg = {
        .port      = BOARD_I2S_PORT,
        .tx_handle = i2s_tx,
        .rx_handle = i2s_rx,
    };
    i2s_data_if = audio_codec_new_i2s_data(&data_cfg);
    if (i2s_data_if == NULL) {
        ESP_LOGE(TAG, "I2S data interface failed");
        abort();
    }

    ESP_ERROR_CHECK(init_es8311());
    ESP_ERROR_CHECK(init_es7210());

    board_set_speaker_enabled(true);
    ESP_LOGI(TAG, "Board init complete — ES8311 (playback) + ES7210 (record) ready");
}

// Bring-up diagnostic: a short tone straight through esp_codec_dev,
// bypassing the entire LiveKit/av_render pipeline. Audible beep at boot =
// codec + PA + speaker wiring are good.
void board_play_test_tone(void)
{
    esp_codec_dev_sample_info_t fs = {
        .sample_rate = 16000,
        .channel = 2,
        .bits_per_sample = 16,
    };
    if (esp_codec_dev_open(play_dev, &fs) != ESP_CODEC_DEV_OK) {
        ESP_LOGE(TAG, "test tone: open failed");
        return;
    }
    enum { FRAMES = 4800 };  // 300 ms
    int16_t *buf = malloc(FRAMES * 2 * sizeof(int16_t));
    if (buf) {
        for (int i = 0; i < FRAMES; i++) {
            // 500 Hz square-ish tone, comfortable level
            int16_t v = ((i / 16) % 2) ? 6000 : -6000;
            buf[2 * i] = v;
            buf[2 * i + 1] = v;
        }
        esp_codec_dev_write(play_dev, buf, FRAMES * 2 * sizeof(int16_t));
        free(buf);
        ESP_LOGI(TAG, "test tone played");
    }
    esp_codec_dev_close(play_dev);
}

esp_codec_dev_handle_t get_playback_handle(void) { return play_dev; }
esp_codec_dev_handle_t get_record_handle(void)   { return rec_dev; }
i2c_master_bus_handle_t board_get_i2c_bus(void)  { return i2c_bus; }
esp_io_expander_handle_t board_get_io_expander(void) { return io_expander; }

void board_set_speaker_enabled(bool enabled)
{
    esp_io_expander_set_level(io_expander, EXIO_PA_EN, enabled ? 1 : 0);
    if (enabled) {
        vTaskDelay(pdMS_TO_TICKS(10));  // NS4150B settle time
    }
}

void board_set_volume(int volume)
{
    if (volume < 0) volume = 0;
    if (volume > 100) volume = 100;
    cur_volume = volume;
    if (play_dev) {
        esp_codec_dev_set_out_vol(play_dev, volume);
    }
}

int board_get_volume(void) { return cur_volume; }

void board_set_mic_muted(bool muted)
{
    mic_muted = muted;
    if (rec_dev) {
        esp_codec_dev_set_in_mute(rec_dev, muted);
    }
}

bool board_get_mic_muted(void) { return mic_muted; }
