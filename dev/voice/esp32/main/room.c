// LiveKit room lifecycle for the voice satellite.
//
// Based on the custom_hardware example's example.c, extended with:
//  - on_participant_info: detect the Minny agent joining/leaving,
//  - on_data_received: agent state pushed by the Python agent on the
//    "agent_state" data topic (the ESP32 SDK cannot read the
//    lk.agent.state participant attribute — protocol v1 + nanopb strips it),
//  - a connection-manager task that (re)connects with a fixed backoff.
//    The SDK handles transient RECONNECTING itself; this task only acts on
//    DISCONNECTED/FAILED, which also covers the SDK's known issue where a
//    remote participant leaving can drop the connection.

#include "room.h"

#include <string.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "livekit.h"

#include "media.h"
#include "ui.h"

static const char *TAG = "room";

#define RECONNECT_BACKOFF_MS 5000

static livekit_room_handle_t room_handle;
static TaskHandle_t          room_task_handle;
static bool                  agent_present;
// Set when the agent participant leaves: the engine holds its single audio
// subscription on the dead track sid forever (only a full reconnect clears
// it), so the room task must close + reconnect to hear the next agent job.
static volatile bool         agent_left_reconnect;

static void on_state_changed(livekit_connection_state_t state, void *ctx)
{
    ESP_LOGI(TAG, "Room state: %s", livekit_connection_state_str(state));

    switch (state) {
        case LIVEKIT_CONNECTION_STATE_CONNECTED:
            ui_post_conn(UI_CONN_CONNECTED);
            break;
        case LIVEKIT_CONNECTION_STATE_CONNECTING:
            ui_post_conn(UI_CONN_CONNECTING);
            break;
        case LIVEKIT_CONNECTION_STATE_RECONNECTING:
            ui_post_conn(UI_CONN_RECONNECTING);
            break;
        case LIVEKIT_CONNECTION_STATE_DISCONNECTED:
            // Benign: the room task reconnects immediately.
            ui_post_conn(UI_CONN_CONNECTING);
            break;
        case LIVEKIT_CONNECTION_STATE_FAILED: {
            livekit_failure_reason_t reason = livekit_room_get_failure_reason(room_handle);
            ESP_LOGE(TAG, "Connection failed: %s", livekit_failure_reason_str(reason));
            ui_post_conn(UI_CONN_FAILED);
            break;
        }
        default:
            ui_post_conn(UI_CONN_FAILED);
            break;
    }
    if (state != LIVEKIT_CONNECTION_STATE_CONNECTED) {
        agent_present = false;
        ui_post_agent(UI_AGENT_NONE);
    }
    if (room_task_handle) {
        xTaskNotifyGive(room_task_handle);
    }
}

static void on_participant_info(const livekit_participant_info_t *info, void *ctx)
{
    if (info->kind != LIVEKIT_PARTICIPANT_KIND_AGENT) {
        return;
    }
    if (info->state == LIVEKIT_PARTICIPANT_STATE_DISCONNECTED) {
        if (agent_present) {
            ESP_LOGI(TAG, "Agent '%s' left the room — scheduling reconnect",
                     info->identity ? info->identity : "?");
            agent_left_reconnect = true;
            if (room_task_handle) {
                xTaskNotifyGive(room_task_handle);
            }
        }
        agent_present = false;
        ui_post_agent(UI_AGENT_NONE);
    } else if (!agent_present) {
        ESP_LOGI(TAG, "Agent '%s' joined the room", info->identity ? info->identity : "?");
        agent_present = true;
        ui_post_agent(UI_AGENT_INITIALIZING);
    }
}

static void on_data_received(const livekit_data_received_t *data, void *ctx)
{
    if (data->topic == NULL || strcmp(data->topic, "agent_state") != 0) {
        return;
    }
    char state[24] = {0};
    size_t len = data->payload.size < sizeof(state) - 1 ? data->payload.size
                                                        : sizeof(state) - 1;
    memcpy(state, data->payload.bytes, len);
    ESP_LOGI(TAG, "Agent state: %s", state);

    if (strcmp(state, "listening") == 0) {
        ui_post_agent(UI_AGENT_LISTENING);
    } else if (strcmp(state, "thinking") == 0) {
        ui_post_agent(UI_AGENT_THINKING);
    } else if (strcmp(state, "speaking") == 0) {
        ui_post_agent(UI_AGENT_SPEAKING);
    } else if (strcmp(state, "initializing") == 0) {
        ui_post_agent(UI_AGENT_INITIALIZING);
    } else {
        ui_post_agent(UI_AGENT_IDLE);
    }
}

// Connect when disconnected; back off after failures; otherwise sit idle
// until a state change (or the periodic check) wakes us.
static void room_task(void *arg)
{
    for (;;) {
        if (agent_left_reconnect) {
            agent_left_reconnect = false;
            if (livekit_room_get_state(room_handle) == LIVEKIT_CONNECTION_STATE_CONNECTED) {
                ESP_LOGI(TAG, "Closing room to resubscribe to the next agent");
                livekit_room_close(room_handle);
                // Fall through: state polling below reconnects.
            }
        }

        livekit_connection_state_t state = livekit_room_get_state(room_handle);
        if (state == LIVEKIT_CONNECTION_STATE_DISCONNECTED ||
            state == LIVEKIT_CONNECTION_STATE_FAILED) {
            if (state == LIVEKIT_CONNECTION_STATE_FAILED) {
                // Auth-class failures won't heal by retrying every 5 s
                // (typically an expired token — see gen_secrets.py).
                livekit_failure_reason_t reason =
                    livekit_room_get_failure_reason(room_handle);
                bool auth = (reason == LIVEKIT_FAILURE_REASON_BAD_TOKEN ||
                             reason == LIVEKIT_FAILURE_REASON_UNAUTHORIZED);
                vTaskDelay(pdMS_TO_TICKS(auth ? 12 * RECONNECT_BACKOFF_MS
                                              : RECONNECT_BACKOFF_MS));
            }
            ESP_LOGI(TAG, "Connecting to %s", CONFIG_LK_SERVER_URL);
            ui_post_conn(UI_CONN_CONNECTING);
            livekit_err_t err = livekit_room_connect(room_handle,
                                                     CONFIG_LK_SERVER_URL,
                                                     CONFIG_LK_TOKEN);
            if (err != LIVEKIT_ERR_NONE) {
                ESP_LOGE(TAG, "livekit_room_connect failed: %d", err);
                ui_post_conn(UI_CONN_FAILED);
                vTaskDelay(pdMS_TO_TICKS(RECONNECT_BACKOFF_MS));
                continue;
            }
        }
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(10000));
    }
}

void room_start(void)
{
    if (room_handle != NULL) {
        ESP_LOGE(TAG, "Room already created");
        return;
    }

    livekit_room_options_t options = {
        .publish = {
            .kind = LIVEKIT_MEDIA_TYPE_AUDIO,
            .audio_encode = {
                .codec = LIVEKIT_AUDIO_CODEC_OPUS,
                .sample_rate = 16000,
                .channel_count = 1,
            },
            .capturer = media_get_capturer(),
        },
        .subscribe = {
            .kind = LIVEKIT_MEDIA_TYPE_AUDIO,
            .renderer = media_get_renderer(),
        },
        .on_state_changed = on_state_changed,
        .on_participant_info = on_participant_info,
        .on_data_received = on_data_received,
    };
    if (livekit_room_create(&room_handle, &options) != LIVEKIT_ERR_NONE) {
        ESP_LOGE(TAG, "Failed to create room");
        ui_post_conn(UI_CONN_FAILED);
        return;
    }

    xTaskCreatePinnedToCore(room_task, "room_mgr", 6144, NULL, 5,
                            &room_task_handle, 0);
}
