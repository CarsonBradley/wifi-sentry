/*
 * wifi-sentry: CSI -> JSON datagram encoder (pure C, no ESP-IDF dependency, host-testable).
 *
 * Wire format (one UDP datagram, UTF-8 JSON, no newline, always <= WS_MAX_DATAGRAM bytes):
 *   {"v":1,"src":"esp32:<node>","seq":<u32>,"t_us":<i64>,"rssi":<int>,"noise":<int|null>,
 *    "channel":<int>,"fmt":"<chip>:<ltf>:<sec>[:ltf1][:d<k>]","csi":[<int8>,...]}
 *
 * csi = raw CSI buffer as signed bytes, (imag, real) per sub-carrier, in ESP-IDF order,
 * except: the first 4 values are always forced to 0 (see csi_encode.c), and oversize
 * buffers are reduced as described by the ":ltf1" / ":d<k>" suffixes.
 *
 * With a shared key the board signs that exact datagram and appends the result as the last member:
 *   {...,"csi":[...],"sig":"<32 lowercase hex>"}
 * sig = first 16 bytes of HMAC-SHA256(key, unsigned datagram bytes). The receiver strips the
 * trailing ',"sig":"<hex>"' member (the last 41 bytes before the closing brace) to get the signed bytes.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define WS_MAX_DATAGRAM   1400   /* hard upper bound on the JSON size, bytes            */
#define WS_MAX_CSI_INTS   234    /* max csi values per datagram (fits HT40 = 117 sc)     */
#define WS_MAX_NODE_NAME  31
#define WS_NOISE_NULL     (-32768)
#define WS_SIG_BYTES      16     /* truncated HMAC-SHA256 carried in "sig" (32 hex chars)  */
#define WS_SIG_GROWTH     41     /* ',"sig":"' + 32 hex + '"' : bytes a signature adds      */

typedef enum {
    WS_ENGINE_LEGACY = 0,   /* ESP32, S2, S3, C3: lltf_en/htltf_en config; we enable LLTF only */
    WS_ENGINE_HE     = 1,   /* C6: CSI of whatever LTF the received PPDU carries          */
    WS_ENGINE_HE_LLTF = 2,  /* C5, C61: acquire_csi_force_lltf=1, lltf_bit_mode=1 (8-bit)  */
} ws_engine_t;

typedef struct {
    const char *chip;       /* short chip tag: "esp32","s2","s3","c3","c5","c6","c61" */
    ws_engine_t engine;
    uint8_t     sec;        /* secondary channel from rx_ctrl: 0 none, 1 above, 2 below */
} ws_csi_meta_t;

/* Copy name into out (size WS_MAX_NODE_NAME+1), replacing chars outside [A-Za-z0-9_.-] with '_'.
 * Empty input becomes "node". */
void ws_sanitize_node_name(const char *in, char *out);

/* Build the fmt tag and (possibly reduced) csi int list.
 * out_vals must hold WS_MAX_CSI_INTS entries. Returns number of values written. */
size_t ws_csi_prepare(const ws_csi_meta_t *meta, const int8_t *buf, size_t len,
                      int8_t *out_vals, char *fmt, size_t fmt_cap);

/* Encode one datagram. node must already be sanitized. noise = WS_NOISE_NULL for null.
 * Returns the JSON length (no NUL counted), or 0 if it would not fit in min(cap-1, WS_MAX_DATAGRAM). */
size_t ws_encode_datagram(char *out, size_t cap, const char *node, uint32_t seq, int64_t t_us,
                          int rssi, int noise, int channel, const ws_csi_meta_t *meta,
                          const int8_t *buf, size_t len);

/* Append the signature of the len-byte datagram in out (which must end with '}'): the closing brace
 * becomes ,"sig":"<hex of mac>"} so the signed bytes are exactly the datagram as it was before.
 * Returns the new length, or 0 if it would not fit in min(cap-1, WS_MAX_DATAGRAM). */
size_t ws_append_sig(char *out, size_t len, size_t cap, const uint8_t mac[WS_SIG_BYTES]);

#ifdef __cplusplus
}
#endif
