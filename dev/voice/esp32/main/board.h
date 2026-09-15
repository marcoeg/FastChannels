#pragma once

#include <stdbool.h>
#include "esp_codec_dev.h"
#include "driver/i2c_master.h"
#include "esp_io_expander.h"

#ifdef __cplusplus
extern "C" {
#endif

// Initialize the Waveshare ESP32-S3-AUDIO-Board: I2C bus, TCA9555 IO
// expander, I2S1 duplex, ES8311 (playback DAC) and ES7210 (mic ADC),
// then enable the NS4150B speaker amplifier.
void board_init(void);

// Playback codec device handle (ES8311). Opened lazily by av_render.
esp_codec_dev_handle_t get_playback_handle(void);

// Record codec device handle (ES7210). Opened by the esp_capture AEC source.
esp_codec_dev_handle_t get_record_handle(void);

// Shared I2C0 bus (codecs, TCA9555, RTC, touch controller all live here).
i2c_master_bus_handle_t board_get_i2c_bus(void);

// TCA9555 IO expander (LCD_RST=EXIO0, TP_RST=EXIO1, TP_INT=EXIO2, PA=EXIO8).
esp_io_expander_handle_t board_get_io_expander(void);

// NS4150B power-amplifier enable (TCA9555 EXIO8, active high).
void board_set_speaker_enabled(bool enabled);

// Bring-up diagnostic: 300 ms tone straight through the codec (no LiveKit).
void board_play_test_tone(void);

// Wake chime: a short rising two-note tone, distinct from the boot beep.
void board_play_wake_chime(void);

// Speaker volume 0..100 (esp_codec_dev software+hardware volume).
void board_set_volume(int volume);
int  board_get_volume(void);

// Microphone mute at the codec level. The LiveKit SDK has no track-mute
// API, so muting means the published track carries silence.
void board_set_mic_muted(bool muted);
bool board_get_mic_muted(void);

#ifdef __cplusplus
}
#endif
