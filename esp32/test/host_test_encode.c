/*
 * Host test for main/csi_encode.c (no ESP-IDF needed). Prints one JSON datagram per line for a
 * matrix of engines x documented CSI buffer lengths x worst-case values, each one unsigned and then
 * with a signature appended (ws_append_sig, fixed test MAC); test/run_host_test.sh pipes the output
 * through tools/udp_dump.py's validator and checks that stripping the sig gives the unsigned line.
 *
 *   cc -std=c11 -Wall -Wextra -Werror -I../main ../main/csi_encode.c host_test_encode.c -o host_test
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "csi_encode.h"

static int8_t buf[640];

static int emit(const char *chip, ws_engine_t eng, uint8_t sec, size_t len, int pattern, const char *name,
                uint32_t seq, int64_t t_us, int noise)
{
    for (size_t i = 0; i < len; i++) {
        buf[i] = pattern == 0 ? (int8_t)-128 : pattern == 1 ? (int8_t)(i % 256 - 128) : (int8_t)((i * 37) % 61 - 30);
    }
    char node[WS_MAX_NODE_NAME + 1];
    ws_sanitize_node_name(name, node);
    char out[WS_MAX_DATAGRAM + 1];
    ws_csi_meta_t meta = { .chip = chip, .engine = eng, .sec = sec };
    size_t n = ws_encode_datagram(out, sizeof out, node, seq, t_us, -127, noise, 165, &meta, buf, len);
    if (n == 0 || n > WS_MAX_DATAGRAM || strlen(out) != n) {
        fprintf(stderr, "FAIL: %s len=%zu pattern=%d -> n=%zu\n", chip, len, pattern, n);
        return 1;
    }
    puts(out);
    uint8_t mac[WS_SIG_BYTES];
    for (size_t i = 0; i < WS_SIG_BYTES; i++) {
        mac[i] = (uint8_t)(0xf0 + i * 17 + seq);   /* every hex digit, both nibbles */
    }
    size_t m = ws_append_sig(out, n, sizeof out, mac);
    if (m != n + WS_SIG_GROWTH || m > WS_MAX_DATAGRAM || strlen(out) != m) {
        fprintf(stderr, "FAIL: sig %s len=%zu pattern=%d -> n=%zu m=%zu\n", chip, len, pattern, n, m);
        return 1;
    }
    puts(out);
    return 0;
}

int main(void)
{
    static const size_t lens[] = { 2, 4, 7, 106, 114, 128, 228, 234, 256, 376, 380, 384, 468, 490, 612, 640 };
    const struct { const char *chip; ws_engine_t eng; } chips[] = {
        { "s3", WS_ENGINE_LEGACY }, { "esp32", WS_ENGINE_LEGACY }, { "c6", WS_ENGINE_HE }, { "c5", WS_ENGINE_HE_LLTF },
    };
    const char *long_name = "a-very-long-node-name-with spaces/and\"quotes\\x";
    int fails = 0;
    for (size_t c = 0; c < sizeof chips / sizeof chips[0]; c++) {
        for (size_t l = 0; l < sizeof lens / sizeof lens[0]; l++) {
            for (int p = 0; p < 3; p++) {
                for (uint8_t sec = 0; sec < 4; sec += 3) {
                    fails += emit(chips[c].chip, chips[c].eng, sec, lens[l], p, long_name, 0xFFFFFFFFu,
                                  INT64_C(9223372036854775807), p == 2 ? WS_NOISE_NULL : -127);
                }
            }
        }
    }
    fails += emit("s3", WS_ENGINE_LEGACY, 0, 128, 2, "", 0, 0, -95);   /* empty name -> "node" */
    char tiny[8] = "{}";
    uint8_t z[WS_SIG_BYTES] = { 0 };
    fails += ws_append_sig(tiny, 2, sizeof tiny, z) != 0;               /* must refuse: no room */
    fails += ws_append_sig(tiny, 1, sizeof tiny, z) != 0;               /* must refuse: no '}' */
    fprintf(stderr, "%s\n", fails ? "host encoder test FAILED" : "host encoder test: all datagrams fit");
    return fails ? 1 : 0;
}
