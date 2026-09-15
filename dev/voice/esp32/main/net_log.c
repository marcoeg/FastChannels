// Network log sink: after WiFi is up, esp_log output goes to a UDP
// collector on the LAN instead of the USB-Serial-JTAG console. Two reasons:
// observing the console over USB resets the device (observer effect), and
// console writes may behave differently with/without a host attached —
// this makes device behavior identical whether or not anyone is watching.

#include "net_log.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "lwip/sockets.h"

static int s_sock = -1;
static struct sockaddr_in s_dst;

static int udp_vprintf(const char *fmt, va_list ap)
{
    char buf[300];
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    if (n > 0 && s_sock >= 0) {
        sendto(s_sock, buf, n < (int)sizeof(buf) ? n : (int)sizeof(buf), 0,
               (struct sockaddr *)&s_dst, sizeof(s_dst));
    }
    return n;
}

void net_log_start(const char *host_ip, int port)
{
    s_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (s_sock < 0) {
        ESP_LOGE("netlog", "socket failed");
        return;
    }
    memset(&s_dst, 0, sizeof(s_dst));
    s_dst.sin_family = AF_INET;
    s_dst.sin_port = htons(port);
    s_dst.sin_addr.s_addr = inet_addr(host_ip);

    ESP_LOGI("netlog", "switching log output to udp://%s:%d", host_ip, port);
    esp_log_set_vprintf(udp_vprintf);
    ESP_LOGI("netlog", "network logging active");
}
