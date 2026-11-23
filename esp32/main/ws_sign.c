/*
 * HMAC-SHA256 of each datagram with the shared key. See ws_sign.h and ../README.md ("Wire format").
 */
#include "ws_sign.h"

#include <string.h>

#include "psa/crypto.h"

static psa_key_id_t s_key;
static bool s_ready;

bool ws_sign_init(const char *key)
{
    size_t n = key ? strlen(key) : 0;
    if (n == 0 || psa_crypto_init() != PSA_SUCCESS) {   /* init is idempotent (IDF already ran it) */
        return false;
    }
    if (s_ready) {
        psa_destroy_key(s_key);
        s_ready = false;
    }
    psa_key_attributes_t attr = PSA_KEY_ATTRIBUTES_INIT;
    psa_set_key_usage_flags(&attr, PSA_KEY_USAGE_SIGN_MESSAGE);
    psa_set_key_algorithm(&attr, PSA_ALG_HMAC(PSA_ALG_SHA_256));
    psa_set_key_type(&attr, PSA_KEY_TYPE_HMAC);         /* bits taken from the key length */
    s_ready = psa_import_key(&attr, (const uint8_t *)key, n, &s_key) == PSA_SUCCESS;
    psa_reset_key_attributes(&attr);
    return s_ready;
}

bool ws_sign(const char *msg, size_t len, uint8_t mac[WS_SIG_BYTES])
{
    uint8_t full[PSA_HASH_LENGTH(PSA_ALG_SHA_256)];
    size_t n = 0;
    if (!s_ready || psa_mac_compute(s_key, PSA_ALG_HMAC(PSA_ALG_SHA_256), (const uint8_t *)msg, len,
                                    full, sizeof full, &n) != PSA_SUCCESS || n != sizeof full) {
        return false;
    }
    memcpy(mac, full, WS_SIG_BYTES);                     /* truncated HMAC (RFC 2104 section 5) */
    return true;
}
