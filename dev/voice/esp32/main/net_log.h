#pragma once

#ifdef __cplusplus
extern "C" {
#endif

// Redirect esp_log output to a UDP collector (call once WiFi has an IP).
void net_log_start(const char *host_ip, int port);

#ifdef __cplusplus
}
#endif
