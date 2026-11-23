/*
 * wifi-sentry ESP32 CSI node.
 *
 * Joins the home Wi-Fi as a station, pings the default gateway at CONFIG_WIFI_SENTRY_PING_HZ,
 * captures the CSI of the router's replies (frames whose source MAC == connected BSSID) and sends
 * each CSI frame to the Mac as one JSON UDP datagram (format: csi_encode.h / README.md), signed
 * with CONFIG_WIFI_SENTRY_SHARED_KEY when one is set (ws_sign.c).
 *
 * Target: ESP-IDF v6.1.x. Chips: ESP32-S3 and ESP32-C6 (primary), ESP32 / C3 / C5 also compile.
 *
 * Threads:
 *   Wi-Fi task  -> csi_rx_cb(): filter + copy into a queue (must stay short, per IDF docs)
 *   sender task -> encode JSON + sendto(); logs a status line every 10 s
 *   event loop  -> connect / reconnect with backoff, start/stop the ping session
 * Wi-Fi loss never reboots the board: it retries forever with 1..30 s backoff.
 */
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "esp_event.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

#include "lwip/inet.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"
#include "ping/ping_sock.h"

#include "csi_encode.h"
#include "ws_sign.h"

#if !CONFIG_ESP_WIFI_CSI_ENABLED
#error "CONFIG_ESP_WIFI_CSI_ENABLED must be y (it is set in sdkconfig.defaults; delete sdkconfig and rebuild)"
#endif

#if ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(6, 1, 0)
#warning "wifi-sentry was written and compiled against ESP-IDF v6.1.1; other versions are untested"
#endif

/* ---------------------------------------------------------------- chip / CSI engine selection */
#if CONFIG_IDF_TARGET_ESP32
#define WS_CHIP "esp32"
#elif CONFIG_IDF_TARGET_ESP32S2
#define WS_CHIP "s2"
#elif CONFIG_IDF_TARGET_ESP32S3
#define WS_CHIP "s3"
#elif CONFIG_IDF_TARGET_ESP32C3
#define WS_CHIP "c3"
#elif CONFIG_IDF_TARGET_ESP32C5
#define WS_CHIP "c5"
#elif CONFIG_IDF_TARGET_ESP32C6
#define WS_CHIP "c6"
#elif CONFIG_IDF_TARGET_ESP32C61
#define WS_CHIP "c61"
#else
#error "Unsupported target for wifi-sentry (no CSI, or untested)"
#endif

#if CONFIG_SOC_WIFI_HE_SUPPORT
#if CONFIG_SOC_WIFI_MAC_VERSION_NUM >= 3
#define WS_ENGINE_KIND 2   /* C5 / C61: can force LLTF in 8-bit mode */
#if ESP_IDF_VERSION < ESP_IDF_VERSION_VAL(6, 1, 0)
#error "ESP32-C5/C61 need ESP-IDF >= 6.1 (lltf_bit_mode) for int8 CSI"
#endif
#else
#define WS_ENGINE_KIND 1   /* C6 */
#endif
#else
#define WS_ENGINE_KIND 0   /* ESP32 / S2 / S3 / C3 */
#endif

/* numeric for #if; the encoder enum uses the same values */
#define WS_ENGINE ((ws_engine_t)WS_ENGINE_KIND)
_Static_assert(WS_ENGINE_LEGACY == 0 && WS_ENGINE_HE == 1 && WS_ENGINE_HE_LLTF == 2, "engine ids");

#define CSI_BUF_MAX   640             /* > 612, the largest buffer ESP-IDF documents */
#define CSI_QUEUE_LEN 16
#define STATUS_PERIOD_US (10 * 1000 * 1000)

static const char *TAG = "wifi-sentry";

typedef struct {
    uint32_t seq;
    int64_t  t_us;
    int8_t   rssi;
    int8_t   noise;      /* 0 = unknown */
    uint8_t  channel;
    uint8_t  sec;
    uint16_t len;
    int8_t   buf[CSI_BUF_MAX];
} csi_item_t;

static QueueHandle_t s_csi_q;
static char s_node[WS_MAX_NODE_NAME + 1];

/* BSSID filter: written by the event loop, read by the Wi-Fi task. */
static portMUX_TYPE s_bssid_lock = portMUX_INITIALIZER_UNLOCKED;
static uint8_t s_bssid[6];
static bool s_bssid_valid;

/* Counters (single writer each; read for logging only). */
static volatile uint32_t s_seq;          /* per accepted CSI frame; gaps = loss anywhere */
static volatile uint32_t s_cb_total, s_cb_accepted, s_q_drops, s_invalid;
static volatile uint32_t s_sent, s_send_err, s_no_target, s_encode_err, s_sign_err;
static volatile int s_last_rssi;
static volatile uint16_t s_last_len;

/* Connection state. */
static esp_ping_handle_t s_ping;
static esp_timer_handle_t s_reconnect_timer;
static uint32_t s_backoff_ms = 1000;
static volatile bool s_need_resolve;
static volatile bool s_have_ip;

/* ---------------------------------------------------------------- CSI callback (Wi-Fi task) */
static void csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    static csi_item_t item;   /* the Wi-Fi task is the only caller: a static avoids stack use */
    (void)ctx;
    s_cb_total++;
    if (!info || !info->buf || info->len == 0) {
        return;
    }

    bool match;
    portENTER_CRITICAL(&s_bssid_lock);
    match = s_bssid_valid && memcmp(info->mac, s_bssid, 6) == 0;
    portEXIT_CRITICAL(&s_bssid_lock);
    if (!match) {
        return;
    }
#if CONFIG_WIFI_SENTRY_DROP_GROUP_FRAMES
    if (info->dmac[0] & 0x01) {          /* broadcast / multicast (beacons, ARP, mDNS...) */
        return;
    }
#endif
    const wifi_pkt_rx_ctrl_t *rx = &info->rx_ctrl;
#if CONFIG_SOC_WIFI_HE_SUPPORT
    if (!rx->rx_channel_estimate_info_vld) {   /* IDF docs: CSI invalid when this is 0 */
        s_invalid++;
        return;
    }
    item.sec = rx->second;
#else
    item.sec = rx->secondary_channel;
#endif
    item.t_us = esp_timer_get_time();
    item.rssi = rx->rssi;
    item.noise = rx->noise_floor;
    item.channel = rx->channel;
    item.len = info->len > CSI_BUF_MAX ? CSI_BUF_MAX : info->len;
    memcpy(item.buf, info->buf, item.len);
    item.seq = s_seq++;
    s_cb_accepted++;
    s_last_rssi = rx->rssi;
    s_last_len = info->len;
    if (xQueueSend(s_csi_q, &item, 0) != pdTRUE) {
        s_q_drops++;
    }
}

static void csi_init(void)
{
    wifi_csi_config_t cfg;
    memset(&cfg, 0, sizeof cfg);
#if WS_ENGINE_KIND == 0
    cfg.lltf_en = true;            /* LLTF only: 128 bytes for every OFDM frame, any PPDU type */
    cfg.htltf_en = false;
    cfg.stbc_htltf2_en = false;
    cfg.ltf_merge_en = false;
    cfg.channel_filter_en = false; /* keep sub-carriers independent; smooth on the Mac */
    cfg.manu_scale = false;        /* automatic scaling: normalise amplitude per frame */
    cfg.shift = 0;
#elif WS_ENGINE_KIND == 1
    cfg.enable = 1;
    cfg.acquire_csi_legacy = 1;
    cfg.acquire_csi_ht20 = 1;
    cfg.acquire_csi_ht40 = 1;      /* only if the AP ignores our HT20 request; encoder reduces */
    cfg.acquire_csi_su = 1;        /* only if 11ax were negotiated; encoder decimates he20 */
    cfg.acquire_csi_mu = 0;
    cfg.acquire_csi_dcm = 0;
    cfg.acquire_csi_beamformed = 1;
    cfg.acquire_csi_he_stbc = 0;   /* complete HE-LTF1 */
    cfg.val_scale_cfg = 0;
    cfg.dump_ack_en = 0;
#else /* WS_ENGINE_HE_LLTF */
    cfg.enable = 1;
    cfg.acquire_csi_legacy = 1;
    cfg.acquire_csi_force_lltf = 1; /* always the L-LTF, whatever the PPDU format */
    cfg.acquire_csi_ht20 = 1;
    cfg.acquire_csi_ht40 = 1;
    cfg.acquire_csi_vht = 1;
    cfg.acquire_csi_su = 1;
    cfg.acquire_csi_mu = 0;
    cfg.acquire_csi_dcm = 0;
    cfg.acquire_csi_beamformed = 1;
    cfg.acquire_csi_he_stbc_mode = 0;
    cfg.val_scale_cfg = 0;
    cfg.dump_ack_en = 0;
    cfg.lltf_bit_mode = 1;          /* 8-bit I/Q, so the buffer is plain int8 pairs */
#endif
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

/* ---------------------------------------------------------------- ping (traffic generator) */
static void ping_stop(void)
{
    if (s_ping) {
        esp_ping_stop(s_ping);
        esp_ping_delete_session(s_ping);
        s_ping = NULL;
    }
}

static void ping_start(const esp_ip4_addr_t *gw)
{
    ping_stop();
    esp_ping_config_t pc = ESP_PING_DEFAULT_CONFIG();
    uint32_t interval = 1000 / CONFIG_WIFI_SENTRY_PING_HZ;
    if (interval == 0) interval = 1;
    pc.count = ESP_PING_COUNT_INFINITE;
    pc.interval_ms = interval;
    pc.timeout_ms = interval;      /* a lost reply must not stall the stream */
    pc.data_size = 8;              /* tiny payload: we only need the reply's preamble */
    pc.task_stack_size = 3072;
    pc.target_addr.type = IPADDR_TYPE_V4;
    pc.target_addr.u_addr.ip4.addr = gw->addr;

    esp_ping_callbacks_t cbs = { 0 };
    if (esp_ping_new_session(&pc, &cbs, &s_ping) != ESP_OK) {
        ESP_LOGE(TAG, "ping session create failed");
        s_ping = NULL;
        return;
    }
    esp_ping_start(s_ping);
    ESP_LOGI(TAG, "pinging gateway " IPSTR " every %" PRIu32 " ms", IP2STR(gw), interval);
}

/* ---------------------------------------------------------------- Wi-Fi connection management */
static void reconnect_cb(void *arg)
{
    (void)arg;
    esp_err_t err = esp_wifi_connect();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "esp_wifi_connect: %s", esp_err_to_name(err));
    }
}

static void schedule_reconnect(void)
{
    esp_timer_stop(s_reconnect_timer);   /* harmless if not running */
    ESP_LOGW(TAG, "reconnecting in %" PRIu32 " ms", s_backoff_ms);
    esp_timer_start_once(s_reconnect_timer, (uint64_t)s_backoff_ms * 1000);
    s_backoff_ms = s_backoff_ms * 2 > 30000 ? 30000 : s_backoff_ms * 2;
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
        const wifi_event_sta_connected_t *e = data;
        portENTER_CRITICAL(&s_bssid_lock);
        memcpy(s_bssid, e->bssid, 6);
        s_bssid_valid = true;
        portEXIT_CRITICAL(&s_bssid_lock);
        ESP_LOGI(TAG, "associated: ssid=%.*s bssid=" MACSTR " channel=%d",
                 e->ssid_len, (const char *)e->ssid, MAC2STR(e->bssid), e->channel);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *e = data;
        portENTER_CRITICAL(&s_bssid_lock);
        s_bssid_valid = false;
        portEXIT_CRITICAL(&s_bssid_lock);
        s_have_ip = false;
        ping_stop();
        ESP_LOGW(TAG, "Wi-Fi disconnected (reason %d)%s", e->reason,
                 e->reason == WIFI_REASON_NO_AP_FOUND ? " - SSID not found (2.4 GHz network?)" :
                 (e->reason == WIFI_REASON_AUTH_FAIL || e->reason == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT)
                     ? " - wrong password?" : "");
        schedule_reconnect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "got ip " IPSTR " gw " IPSTR, IP2STR(&e->ip_info.ip), IP2STR(&e->ip_info.gw));
        s_backoff_ms = 1000;
        s_have_ip = true;
        s_need_resolve = true;
        ping_start(&e->ip_info.gw);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_LOST_IP) {
        ESP_LOGW(TAG, "lost ip");
        s_have_ip = false;
        ping_stop();
    }
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));   /* credentials come from sdkconfig only */
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));

    const esp_timer_create_args_t targs = { .callback = reconnect_cb, .name = "ws_reconnect" };
    ESP_ERROR_CHECK(esp_timer_create(&targs, &s_reconnect_timer));

    wifi_config_t wc;
    memset(&wc, 0, sizeof wc);
    strlcpy((char *)wc.sta.ssid, CONFIG_WIFI_SENTRY_WIFI_SSID, sizeof wc.sta.ssid);
    strlcpy((char *)wc.sta.password, CONFIG_WIFI_SENTRY_WIFI_PASSWORD, sizeof wc.sta.password);
    wc.sta.threshold.authmode = strlen(CONFIG_WIFI_SENTRY_WIFI_PASSWORD) ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wc.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
    wc.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;          /* mesh / multi-AP: pick the strongest */
    wc.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    wc.sta.pmf_cfg.capable = true;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));

#if CONFIG_SOC_WIFI_HE_SUPPORT
    ESP_ERROR_CHECK(esp_wifi_start());
    /* 11b/g/n only, 20 MHz: the router then answers with HT20 frames -> fixed 114-byte CSI on C6.
     * (11ax would give 490-byte HE CSI; 40 MHz would give 234/468 bytes.) */
    wifi_protocols_t protos = { .ghz_2g = WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N,
#if CONFIG_SOC_WIFI_SUPPORT_5G
                                .ghz_5g = WIFI_PROTOCOL_11A | WIFI_PROTOCOL_11N,
#endif
                              };
    wifi_bandwidths_t bws = { .ghz_2g = WIFI_BW20,
#if CONFIG_SOC_WIFI_SUPPORT_5G
                              .ghz_5g = WIFI_BW20,
#endif
                            };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(WIFI_IF_STA, &protos));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(WIFI_IF_STA, &bws));
#else
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW20));
    ESP_ERROR_CHECK(esp_wifi_start());
#endif
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));          /* power save would starve CSI */
    csi_init();

    if (strlen(CONFIG_WIFI_SENTRY_WIFI_SSID) == 0) {
        ESP_LOGE(TAG, "No SSID configured. Run `idf.py menuconfig` -> wifi-sentry, then rebuild and flash.");
        return;
    }
    ESP_LOGI(TAG, "connecting to \"%s\"", CONFIG_WIFI_SENTRY_WIFI_SSID);
    esp_wifi_connect();
}

/* ---------------------------------------------------------------- sender task */
static bool resolve_target(struct sockaddr_in *dst)
{
    if (strlen(CONFIG_WIFI_SENTRY_TARGET_HOST) == 0) {
        ESP_LOGE(TAG, "No receiver address configured. Run `idf.py menuconfig` -> wifi-sentry -> "
                      "Receiver (Mac) IP address, then rebuild and flash.");
        return false;
    }
    struct addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_DGRAM };
    struct addrinfo *res = NULL;
    int err = getaddrinfo(CONFIG_WIFI_SENTRY_TARGET_HOST, NULL, &hints, &res);
    if (err != 0 || !res) {
        ESP_LOGE(TAG, "cannot resolve target \"%s\" (err %d)", CONFIG_WIFI_SENTRY_TARGET_HOST, err);
        return false;
    }
    memcpy(dst, res->ai_addr, sizeof *dst);
    dst->sin_port = htons(CONFIG_WIFI_SENTRY_TARGET_PORT);
    freeaddrinfo(res);
    char ip[16];
    inet_ntoa_r(dst->sin_addr, ip, sizeof ip);
    ESP_LOGI(TAG, "sending CSI to %s:%d as src \"esp32:%s\"", ip, CONFIG_WIFI_SENTRY_TARGET_PORT, s_node);
    return true;
}

static void sender_task(void *arg)
{
    (void)arg;
    static csi_item_t item;
    static char json[WS_MAX_DATAGRAM + 1];
    const ws_csi_meta_t base_meta = { .chip = WS_CHIP, .engine = WS_ENGINE, .sec = 0 };

    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno %d", errno);
        vTaskDelete(NULL);
        return;
    }
    struct sockaddr_in dst = { 0 };
    bool have_dst = false;
    int64_t last_resolve_try = 0, last_status = esp_timer_get_time();
    uint32_t last_sent = 0;
    char last_fmt[48] = "-";
    const bool sign = strlen(CONFIG_WIFI_SENTRY_SHARED_KEY) > 0;
    if (sign && !ws_sign_init(CONFIG_WIFI_SENTRY_SHARED_KEY)) {
        /* never fall back to unsigned: the Mac would drop it anyway, and silence is easier to spot */
        ESP_LOGE(TAG, "cannot set up the shared key; nothing will be sent");
    }
    ESP_LOGI(TAG, "datagrams are %s", sign ? "signed with the shared key" : "UNSIGNED (no shared key set)");

    for (;;) {
        int64_t now = esp_timer_get_time();
        if (s_have_ip && (s_need_resolve || (!have_dst && now - last_resolve_try > 5000000))) {
            s_need_resolve = false;
            last_resolve_try = now;
            have_dst = resolve_target(&dst);
        }
        if (now - last_status >= STATUS_PERIOD_US) {
            float hz = (s_sent - last_sent) * 1e6f / (float)(now - last_status);
            ESP_LOGI(TAG, "status: %s, sent %.1f/s total %" PRIu32 ", csi cb %" PRIu32 " kept %" PRIu32
                     ", queue drops %" PRIu32 ", send errors %" PRIu32 ", sign errors %" PRIu32
                     ", rssi %d, last fmt %s len %u",
                     s_have_ip ? "online" : "OFFLINE", hz, s_sent, s_cb_total, s_cb_accepted, s_q_drops,
                     s_send_err, s_sign_err, s_last_rssi, last_fmt, s_last_len);
            last_status = now;
            last_sent = s_sent;
        }

        if (xQueueReceive(s_csi_q, &item, pdMS_TO_TICKS(1000)) != pdTRUE) {
            continue;
        }
        if (!have_dst) {
            s_no_target++;
            continue;
        }
        ws_csi_meta_t meta = base_meta;
        meta.sec = item.sec;
        int noise = (item.noise <= -30 && item.noise >= -120) ? item.noise : WS_NOISE_NULL;
        size_t n = ws_encode_datagram(json, sizeof json, s_node, item.seq, item.t_us, item.rssi, noise,
                                      item.channel, &meta, item.buf, item.len);
        if (n == 0) {
            s_encode_err++;
            continue;
        }
        if (sign) {   /* fmt is read below from the signed text: the sig only replaces the final '}' */
            uint8_t mac[WS_SIG_BYTES];
            n = ws_sign(json, n, mac) ? ws_append_sig(json, n, sizeof json, mac) : 0;
            if (n == 0) {
                s_sign_err++;
                continue;
            }
        }
        const char *f = strstr(json, "\"fmt\":\"");
        if (f) {
            f += 7;
            size_t fl = strcspn(f, "\"");
            if (fl >= sizeof last_fmt) fl = sizeof last_fmt - 1;
            memcpy(last_fmt, f, fl);
            last_fmt[fl] = '\0';
        }
        if (sendto(sock, json, n, 0, (struct sockaddr *)&dst, sizeof dst) < 0) {
            s_send_err++;
            if (errno == ENOMEM) {
                vTaskDelay(pdMS_TO_TICKS(2));   /* lwIP out of buffers: back off briefly */
            }
        } else {
            s_sent++;
        }
    }
}

/* ---------------------------------------------------------------- entry point */
void app_main(void)
{
    esp_err_t err = nvs_flash_init();     /* the Wi-Fi driver needs NVS for PHY calibration data */
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ws_sanitize_node_name(CONFIG_WIFI_SENTRY_NODE_NAME, s_node);
    ESP_LOGI(TAG, "wifi-sentry node \"%s\" on %s (IDF %s), %d Hz -> %s:%d", s_node, WS_CHIP,
             esp_get_idf_version(), CONFIG_WIFI_SENTRY_PING_HZ, CONFIG_WIFI_SENTRY_TARGET_HOST,
             CONFIG_WIFI_SENTRY_TARGET_PORT);

    s_csi_q = xQueueCreate(CSI_QUEUE_LEN, sizeof(csi_item_t));
    configASSERT(s_csi_q);
    xTaskCreate(sender_task, "ws_sender", 6144, NULL, 5, NULL);
    wifi_init();
}
