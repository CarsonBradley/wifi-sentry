/*
 * wifi-sentry: datagram signing with the shared key (HMAC-SHA256 via the PSA Crypto API, which is
 * what ESP-IDF v6 / Mbed TLS 4 provide). Kept apart from main.c so it can be checked on the host
 * against Python's hmac module (see test/run_host_test.sh).
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "csi_encode.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Import key (a NUL-terminated string, used as raw bytes). Returns false for an empty key or on error. */
bool ws_sign_init(const char *key);

/* mac = first WS_SIG_BYTES bytes of HMAC-SHA256(key, msg). Returns false on error. */
bool ws_sign(const char *msg, size_t len, uint8_t mac[WS_SIG_BYTES]);

#ifdef __cplusplus
}
#endif
