#!/bin/sh
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Build an erlc-compatible standalone executable running on AtomVM with
# AOT-compiled (aarch64) native code.
#
# Pipeline:
#   1. erlc the front-end module (atomvm_erlc.erl)
#   2. collect the OTP compiler application beams and the stdlib beams that
#      AtomVM's atomvmlib does not provide
#   3. jit_precompile everything for the target arch
#   4. packbeam with the (already precompiled) atomvmlib
#   5. append the avm + 16-byte trailer to the AtomVM binary
#
# Env overrides: ATOMVM_BUILD (default ~/AtomVM/build.release), TARGET, OTP_LIB

set -e

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ATOMVM_BUILD="${ATOMVM_BUILD:-$HOME/AtomVM/build.release}"
TARGET="${TARGET:-aarch64}"
OTP_LIB="${OTP_LIB:-/opt/local/lib/erlang/lib}"
PATH="/opt/local/bin:$PATH"
export PATH

OUT="$HERE/_build"
AOT="$OUT/aot"
rm -rf "$OUT"
mkdir -p "$AOT" "$OUT/ebin"

JIT_BEAMS="$ATOMVM_BUILD/libs/jit/src/beams"
PACKBEAM="$ATOMVM_BUILD/tools/packbeam/packbeam"
ATOMVM="$ATOMVM_BUILD/src/AtomVM"
ATOMVMLIB="$ATOMVM_BUILD/libs/atomvmlib-$TARGET.avm"

# --- 1. compile the front-end ------------------------------------------------
erlc -o "$OUT/ebin" "$HERE/src/atomvm_erlc.erl"

# --- 2. collect OTP beams ----------------------------------------------------
COMPILER_EBIN=$(ls -d "$OTP_LIB"/compiler-*/ebin | head -1)
STDLIB_EBIN=$(ls -d "$OTP_LIB"/stdlib-*/ebin | head -1)

# stdlib modules the compiler/epp path needs that atomvmlib does not provide
STDLIB_MODULES="${STDLIB_MODULES:-erl_scan erl_parse erl_lint erl_anno epp
    erl_internal erl_features erl_bits otp_internal erl_eval eval_bits erl_error
    ordsets orddict dict gb_sets gb_trees digraph digraph_utils sofs
    beam_lib filelib graph records erl_expand_records erl_pp rand
    io_lib io_lib_format io_lib_fread io_lib_pretty string unicode_util}"

cp "$COMPILER_EBIN"/*.beam "$OUT/ebin/"
for m in $STDLIB_MODULES; do
    cp "$STDLIB_EBIN/$m.beam" "$OUT/ebin/"
done

# --- 3. AOT precompile ---------------------------------------------------------
echo "==> jit_precompile $TARGET ($(ls "$OUT/ebin" | wc -l | tr -d ' ') beams)"
erl -pa "$JIT_BEAMS" -noshell -s jit_precompile -s init stop -- \
    "$TARGET" "$AOT/" "$OUT/ebin"/*.beam

# --- 4. pack -------------------------------------------------------------------
echo "==> packbeam"
"$PACKBEAM" create --start atomvm_erlc "$OUT/erlc.avm" \
    "$AOT"/*.beam "$ATOMVMLIB"

# --- 5. append to the AtomVM binary with trailer -------------------------------
EXE="$OUT/erlc"
cp "$ATOMVM" "$EXE"
cat "$OUT/erlc.avm" >> "$EXE"
SIZE=$(wc -c < "$OUT/erlc.avm" | tr -d ' ')
python3 - "$EXE" "$SIZE" <<'EOF'
import struct, sys
with open(sys.argv[1], 'ab') as f:
    f.write(struct.pack('<Q', int(sys.argv[2])))
    f.write(b'ATOMVMv1')
EOF
chmod +x "$EXE"
echo "Built $EXE"
