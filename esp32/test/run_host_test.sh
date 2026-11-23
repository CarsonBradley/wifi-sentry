#!/bin/sh
# Compile the firmware's JSON encoder for the Mac and validate everything it emits with
# tools/udp_dump.py's schema checker. Needs only the Xcode command-line tools (cc) and python3.
#
#   sh test/run_host_test.sh          encoder + signature framing (no ESP-IDF needed)
#   sh test/run_host_test.sh --psa    also check main/ws_sign.c's HMAC against Python's, using the
#                                     PSA Crypto library from ESP-IDF (run `get_idf` first; ~15 s)
set -e
cd "$(dirname "$0")"
cc -std=c11 -Wall -Wextra -Werror -I../main ../main/csi_encode.c host_test_encode.c -o host_test
./host_test > host_test.out
python3 - host_test.out <<'PY'
import sys
sys.path.insert(0, "../tools")
from udp_dump import split_sig, validate
bad = 0
lines = open(sys.argv[1], "rb").read().splitlines()
fmts = {}
for unsigned, signed in zip(lines[0::2], lines[1::2]):
    for raw in (unsigned, signed):
        obj, errs, warns = validate(raw)
        if errs or warns:
            bad += 1
            print("FAIL", errs, warns, raw[:120])
            break
    else:
        # the receiver's view: stripping the sig member gives back exactly the bytes that were signed
        if split_sig(signed) is None or split_sig(signed)[0] != unsigned or split_sig(unsigned) is not None:
            bad += 1
            print("FAIL sig framing", signed[-60:])
        assert obj["csi"][:4] == [0, 0, 0, 0][:len(obj["csi"])], obj["fmt"]
        fmts[obj["fmt"].split(":", 1)[0] + ":" + obj["fmt"].split(":", 1)[1].replace(":n", "").replace(":x", "")] = len(obj["csi"])
print("validated %d datagrams (%d signed), %d failures, largest %d bytes unsigned, %d signed" % (
    len(lines), len(lines) // 2, bad, max(len(l) for l in lines[0::2]), max(len(l) for l in lines[1::2])))
for k in sorted(fmts):
    print("  %-22s -> %3d csi values" % (k, fmts[k]))
sys.exit(1 if bad else 0)
PY
rm -f host_test host_test.out

[ "${1:-}" = "--psa" ] || exit 0
# Build TF-PSA-Crypto from the ESP-IDF tree for the Mac in a temporary directory (the IDF tree is only
# read). IDF's copy includes "mbedtls/bignum.h" / "ecp.h" from its port layer: two one-line stand-ins.
: "${IDF_PATH:?run get_idf first}"
TF="$IDF_PATH/components/mbedtls/mbedtls/tf-psa-crypto"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/shim/mbedtls"
printf '#pragma once\n#include "mbedtls/private/bignum.h"\n' > "$W/shim/mbedtls/bignum.h"
printf '#pragma once\n#include "mbedtls/private/ecp.h"\n' > "$W/shim/mbedtls/ecp.h"
cmake -S "$TF" -B "$W/build" -G Ninja -DGEN_FILES=OFF -DENABLE_TESTING=OFF -DENABLE_PROGRAMS=OFF \
      -DTF_PSA_CRYPTO_FATAL_WARNINGS=OFF -DCMAKE_BUILD_TYPE=Release "-DCMAKE_C_FLAGS=-I$W/shim" > "$W/cmake.log"
cmake --build "$W/build" --target tfpsacrypto > "$W/build.log"
LIBS=$(find "$W/build" -name '*.a' | sort)
cc -std=c11 -Wall -Wextra -Werror -I../main -I"$TF/include" -I"$TF/drivers/builtin/include" -I"$W/build/include" \
   ../main/csi_encode.c ../main/ws_sign.c host_test_sign.c $LIBS -o "$W/host_sign"
"$W/host_sign" > "$W/sign.out"
python3 - "$W/sign.out" <<'PY'
import sys
sys.path.insert(0, "../tools")
from udp_dump import validate
rows = [l.split(b"\t", 1) for l in open(sys.argv[1], "rb").read().splitlines()]
bad = 0
for key, raw in rows:
    _, errs, warns = validate(raw, key)
    if errs or warns:
        bad += 1
        print("FAIL", key, errs, warns)
    if not validate(raw, key + b"x")[1]:
        bad += 1
        print("FAIL: wrong key accepted", key)
print("PSA HMAC vs Python hmac: %d datagrams, %d keys, %d failures" % (len(rows), len({k for k, _ in rows}), bad))
sys.exit(1 if bad else 0)
PY
