/*
 * wifi-sentry CSI datagram encoder. See csi_encode.h and ../README.md ("Wire format").
 *
 * Deterministic rules applied to every CSI buffer, in this order:
 *  1. The first 4 int8 values (sub-carrier slots 0 and 1) are set to 0. ESP-IDF flags them
 *     invalid on many frames (first_word_invalid, a hardware limitation); zeroing them always
 *     keeps the layout identical frame-to-frame. In the "lltf:n" layout slot 0 is the DC null
 *     anyway, so only sub-carrier +1 is lost.
 *  2. The LTF tag is derived from the engine and the buffer length (table below).
 *  3. If the buffer holds more than WS_MAX_CSI_INTS values:
 *       a. two-LTF (STBC, "x2") layouts keep only the first LTF      -> fmt suffix ":ltf1"
 *       b. if still too long, keep every k-th sub-carrier (k minimal) -> fmt suffix ":d<k>"
 *     With the firmware's default radio settings (HT20, 11ax off on C6) neither happens.
 */
#include "csi_encode.h"

#include <stdio.h>
#include <string.h>

void ws_sanitize_node_name(const char *in, char *out)
{
    size_t n = 0;
    if (in) {
        for (; in[n] && n < WS_MAX_NODE_NAME; n++) {
            char c = in[n];
            int ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                     c == '-' || c == '_' || c == '.';
            out[n] = ok ? c : '_';
        }
    }
    if (n == 0) {
        strcpy(out, "node");
        return;
    }
    out[n] = '\0';
}

/* LTF tag from engine + length. *two_ltf set when the buffer is two equal LTF blocks. */
static const char *ltf_tag(ws_engine_t engine, size_t len, int *two_ltf, char *rawbuf, size_t rawcap)
{
    *two_ltf = 0;
    switch (engine) {
    case WS_ENGINE_LEGACY:
        if (len == 128) return "lltf";               /* 64 sc, LLTF only (we disable HT-LTF) */
        break;
    case WS_ENGINE_HE_LLTF:
        if (len == 106) return "lltf";               /* 53 sc, forced LLTF, 8-bit mode */
        break;
    case WS_ENGINE_HE:
        switch (len) {
        case 106: return "lltf";                     /* 11a/g PPDU: 53 sc               */
        case 114: return "ht20";                     /* HT20: 57 sc                     */
        case 228: *two_ltf = 1; return "ht20x2";     /* HT20 STBC: HT-LTF1 + HT-LTF2    */
        case 234: return "ht40";                     /* HT40: 117 sc                    */
        case 468: *two_ltf = 1; return "ht40x2";     /* HT40 STBC                       */
        case 490: return "he20";                     /* HE20 SU: 245 sc                 */
        default: break;
        }
        break;
    }
    snprintf(rawbuf, rawcap, "raw%u", (unsigned)len);
    return rawbuf;
}

size_t ws_csi_prepare(const ws_csi_meta_t *meta, const int8_t *buf, size_t len,
                      int8_t *out_vals, char *fmt, size_t fmt_cap)
{
    char raw[16];
    int two_ltf = 0;
    len &= ~(size_t)1;                               /* whole (imag, real) pairs only */
    const char *ltf = ltf_tag(meta->engine, len, &two_ltf, raw, sizeof raw);
    const char sec = meta->sec == 0 ? 'n' : meta->sec == 1 ? 'a' : meta->sec == 2 ? 'b' : 'x';

    size_t n = len;
    char suffix[16] = "";
    if (n > WS_MAX_CSI_INTS && two_ltf) {
        n /= 2;
        strcat(suffix, ":ltf1");
    }
    size_t k = 1;
    if (n > WS_MAX_CSI_INTS) {
        size_t nsc = n / 2, max_sc = WS_MAX_CSI_INTS / 2;
        k = (nsc + max_sc - 1) / max_sc;
        snprintf(suffix + strlen(suffix), sizeof suffix - strlen(suffix), ":d%u", (unsigned)k);
    }
    snprintf(fmt, fmt_cap, "%s:%s:%c%s", meta->chip, ltf, sec, suffix);

    size_t out = 0;
    for (size_t sc = 0; sc * 2 + 1 < n && out + 2 <= WS_MAX_CSI_INTS; sc += k) {
        out_vals[out++] = buf[sc * 2];
        out_vals[out++] = buf[sc * 2 + 1];
    }
    for (size_t i = 0; i < 4 && i < out; i++) {      /* rule 1: slots 0 and 1 always zero */
        out_vals[i] = 0;
    }
    return out;
}

typedef struct {
    char *p;
    size_t cap, len;
    int overflow;
} sb_t;

static void sb_put(sb_t *b, const char *s)
{
    size_t n = strlen(s);
    if (b->len + n >= b->cap) {
        b->overflow = 1;
        return;
    }
    memcpy(b->p + b->len, s, n);
    b->len += n;
    b->p[b->len] = '\0';
}

static void sb_int(sb_t *b, long long v)
{
    char tmp[24];
    char *e = tmp + sizeof tmp - 1, *s = e;
    *e = '\0';
    unsigned long long u = v < 0 ? (unsigned long long)(-(v + 1)) + 1 : (unsigned long long)v;
    do {
        *--s = (char)('0' + (u % 10));
        u /= 10;
    } while (u);
    if (v < 0) *--s = '-';
    sb_put(b, s);
}

size_t ws_encode_datagram(char *out, size_t cap, const char *node, uint32_t seq, int64_t t_us,
                          int rssi, int noise, int channel, const ws_csi_meta_t *meta,
                          const int8_t *buf, size_t len)
{
    int8_t vals[WS_MAX_CSI_INTS];
    char fmt[48];
    size_t nvals = ws_csi_prepare(meta, buf, len, vals, fmt, sizeof fmt);

    if (cap > WS_MAX_DATAGRAM + 1) cap = WS_MAX_DATAGRAM + 1;
    sb_t b = { .p = out, .cap = cap, .len = 0, .overflow = 0 };
    if (cap == 0) return 0;
    out[0] = '\0';

    sb_put(&b, "{\"v\":1,\"src\":\"esp32:");
    sb_put(&b, node);
    sb_put(&b, "\",\"seq\":");
    sb_int(&b, (long long)seq);
    sb_put(&b, ",\"t_us\":");
    sb_int(&b, (long long)t_us);
    sb_put(&b, ",\"rssi\":");
    sb_int(&b, rssi);
    sb_put(&b, ",\"noise\":");
    if (noise == WS_NOISE_NULL) sb_put(&b, "null");
    else sb_int(&b, noise);
    sb_put(&b, ",\"channel\":");
    sb_int(&b, channel);
    sb_put(&b, ",\"fmt\":\"");
    sb_put(&b, fmt);
    sb_put(&b, "\",\"csi\":[");
    for (size_t i = 0; i < nvals; i++) {
        if (i) sb_put(&b, ",");
        sb_int(&b, vals[i]);
    }
    sb_put(&b, "]}");
    return b.overflow ? 0 : b.len;
}

size_t ws_append_sig(char *out, size_t len, size_t cap, const uint8_t mac[WS_SIG_BYTES])
{
    static const char hex[] = "0123456789abcdef";
    if (cap > WS_MAX_DATAGRAM + 1) cap = WS_MAX_DATAGRAM + 1;
    if (len == 0 || out[len - 1] != '}' || len + WS_SIG_GROWTH + 1 > cap) return 0;
    char *p = out + len - 1;                         /* overwrite the closing brace */
    memcpy(p, ",\"sig\":\"", 8);
    p += 8;
    for (size_t i = 0; i < WS_SIG_BYTES; i++) {
        *p++ = hex[mac[i] >> 4];
        *p++ = hex[mac[i] & 15];
    }
    *p++ = '"';
    *p++ = '}';
    *p = '\0';
    return len + WS_SIG_GROWTH;
}
