/*
 * Host test for main/ws_sign.c against the real PSA Crypto library (TF-PSA-Crypto from ESP-IDF's
 * mbedtls component, built for the Mac by `run_host_test.sh --psa`). Prints "<key>\t<signed datagram>"
 * per line; the script checks every sig with Python's hmac module.
 */
#include <stdio.h>
#include <string.h>

#include "csi_encode.h"
#include "ws_sign.h"

int main(void)
{
    static char k64[65], k100[101];
    memset(k64, 'b', 64);      /* exactly one SHA-256 block */
    memset(k100, 'c', 100);    /* longer than a block: HMAC hashes it first (RFC 2104) */
    const char *keys[] = { "k", "0123456789abcdef", k64, k100 };
    static int8_t buf[128];
    int fails = 0;
    for (size_t k = 0; k < sizeof keys / sizeof keys[0]; k++) {
        if (!ws_sign_init(keys[k])) {
            fprintf(stderr, "FAIL: key %zu\n", k);
            return 1;
        }
        for (uint32_t seq = 0; seq < 3; seq++) {
            for (size_t i = 0; i < sizeof buf; i++) buf[i] = (int8_t)(i * 7 + seq * 13 + k);
            char out[WS_MAX_DATAGRAM + 1];
            ws_csi_meta_t meta = { .chip = "s3", .engine = WS_ENGINE_LEGACY, .sec = 0 };
            size_t n = ws_encode_datagram(out, sizeof out, "signtest", seq, 1000000 + seq, -50, -95, 6, &meta,
                                          buf, sizeof buf);
            uint8_t mac[WS_SIG_BYTES];
            n = n && ws_sign(out, n, mac) ? ws_append_sig(out, n, sizeof out, mac) : 0;
            if (n == 0) {
                fails++;
                continue;
            }
            printf("%s\t%s\n", keys[k], out);
        }
    }
    fprintf(stderr, "%s\n", fails ? "host sign test FAILED" : "host sign test: signed");
    return fails ? 1 : 0;
}
