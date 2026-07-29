#!/bin/sh
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Build an erlc-compatible standalone AtomVM executable, in either AOT
# (JIT-precompiled native) or emulated (plain bytecode) mode, against a given
# AtomVM build directory. Generalises build_erlc.sh so the same front-end /
# OTP-beam bundle can be packed for any point of the benchmark build matrix.
#
# Usage:
#   build_erlc_variant.sh <mode> <atomvm-build-dir> <out-exe>
#     mode             aot | emu
#     atomvm-build-dir e.g. ~/AtomVM/build.release (JIT) or .../build.emu (no JIT)
#     out-exe          path of the self-contained erlc executable to produce
#
# Env overrides: TARGET (aot only, default aarch64), OTP_LIB

set -e

MODE="$1"
ATOMVM_BUILD="$2"
OUT_EXE="$3"
if [ -z "$MODE" ] || [ -z "$ATOMVM_BUILD" ] || [ -z "$OUT_EXE" ]; then
    echo "usage: $0 <aot|emu> <atomvm-build-dir> <out-exe>" >&2
    exit 1
fi

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET="${TARGET:-aarch64}"
OTP_LIB="${OTP_LIB:-/opt/local/lib/erlang/lib}"
PATH="/opt/local/bin:$PATH"
export PATH

# Per-variant scratch dir so parallel/sequential variants don't clobber.
OUT="$HERE/_build/variant-$(basename "$OUT_EXE")"
AOT="$OUT/aot"
rm -rf "$OUT"
mkdir -p "$AOT" "$OUT/ebin"

JIT_BEAMS="$ATOMVM_BUILD/libs/jit/src/beams"
PACKBEAM="$ATOMVM_BUILD/tools/packbeam/packbeam"
ATOMVM="$ATOMVM_BUILD/src/AtomVM"
if [ "$MODE" = "aot" ]; then
    ATOMVMLIB="$ATOMVM_BUILD/libs/atomvmlib-$TARGET.avm"
else
    ATOMVMLIB="$ATOMVM_BUILD/libs/atomvmlib.avm"
fi

for f in "$PACKBEAM" "$ATOMVM" "$ATOMVMLIB"; do
    [ -e "$f" ] || { echo "error: missing artifact: $f" >&2; exit 1; }
done

# --- 1. compile the front-end ------------------------------------------------
erlc -o "$OUT/ebin" "$HERE/src/atomvm_erlc.erl"

# --- 2. collect OTP beams ----------------------------------------------------
COMPILER_EBIN=$(ls -d "$OTP_LIB"/compiler-*/ebin | head -1)
STDLIB_EBIN=$(ls -d "$OTP_LIB"/stdlib-*/ebin | head -1)

STDLIB_MODULES="${STDLIB_MODULES:-erl_scan erl_parse erl_lint erl_anno epp
    erl_internal erl_features erl_bits otp_internal erl_eval eval_bits erl_error
    ordsets orddict dict gb_sets gb_trees digraph digraph_utils sofs
    beam_lib filelib erl_expand_records erl_pp rand
    io_lib io_lib_format io_lib_fread io_lib_pretty string unicode_util
    ms_transform}"

# stdlib modules that exist only in some releases: graph and records are new in
# OTP 29, where the compiler uses them. Copied when the toolchain has them and
# skipped otherwise, so the same script builds against OTP 27, 28 and 29 -- an
# unconditional cp would simply fail on the two older ones.
STDLIB_MODULES_OPTIONAL="${STDLIB_MODULES_OPTIONAL:-graph records}"

cp "$COMPILER_EBIN"/*.beam "$OUT/ebin/"
for m in $STDLIB_MODULES; do
    cp "$STDLIB_EBIN/$m.beam" "$OUT/ebin/"
done
for m in $STDLIB_MODULES_OPTIONAL; do
    if [ -f "$STDLIB_EBIN/$m.beam" ]; then
        cp "$STDLIB_EBIN/$m.beam" "$OUT/ebin/"
    fi
done

# --- 3. (aot) precompile, or (emu) keep plain beams --------------------------
if [ "$MODE" = "aot" ]; then
    echo "==> jit_precompile $TARGET ($(ls "$OUT/ebin" | wc -l | tr -d ' ') beams)"
    erl -pa "$JIT_BEAMS" -noshell -s jit_precompile -s init stop -- \
        "$TARGET" "$AOT/" "$OUT/ebin"/*.beam
    PACK_DIR="$AOT"
else
    echo "==> emulated mode: $(ls "$OUT/ebin" | wc -l | tr -d ' ') plain beams"
    PACK_DIR="$OUT/ebin"
fi

# --- 4. pack -----------------------------------------------------------------
echo "==> packbeam"
"$PACKBEAM" create --start atomvm_erlc "$OUT/erlc.avm" \
    "$PACK_DIR"/*.beam "$ATOMVMLIB"

# --- 5. append the avm to the AtomVM binary with trailer ---------------------
mkdir -p "$(dirname -- "$OUT_EXE")"
cp "$ATOMVM" "$OUT_EXE"
cat "$OUT/erlc.avm" >> "$OUT_EXE"
SIZE=$(wc -c < "$OUT/erlc.avm" | tr -d ' ')
python3 - "$OUT_EXE" "$SIZE" <<'EOF'
import struct, sys
with open(sys.argv[1], 'ab') as f:
    f.write(struct.pack('<Q', int(sys.argv[2])))
    f.write(b'ATOMVMv1')
EOF
chmod +x "$OUT_EXE"
echo "Built $OUT_EXE ($MODE, $(basename "$ATOMVM_BUILD"))"
