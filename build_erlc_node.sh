#!/bin/sh
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Build a Node.js flavour of the erlc-compatible front-end: the same bundle of
# AOT-compiled code as the native binary, but targeting wasm32 and run on
# AtomVM's Emscripten build under Node instead of a native executable.
#
# Output (_build/node/):
#   erlc.mjs     -- the launcher; run as `node erlc.mjs [erlc options] file.erl`
#   erlc.avm     -- wasm32-precompiled front-end + OTP compiler + emscripten lib
#   AtomVM.mjs   -- the Emscripten AtomVM runtime (JIT enabled, runs wasm32 AOT)
#   AtomVM.wasm  -- its WebAssembly
#
# Why a launcher rather than argv: AtomVM's Emscripten platform does not forward
# the process argv to the Erlang program, so erlc.mjs passes the command line
# through the ATOMVM_ERLC_ARGV environment variable, which atomvm_erlc:start/0
# reads (see src/atomvm_erlc.erl). The Node build uses -sNODERAWFS, so the real
# working directory and filesystem are visible: sources are read and .beam files
# written exactly as with the native binary.
#
# Env overrides:
#   ATOMVM_BUILD    host AtomVM build tree (default ~/AtomVM/build.release),
#                   configured with wasm32 in AVM_PRECOMPILED_TARGETS so that
#                   libs/atomvmlib-emscripten-wasm32.avm exists. Provides the
#                   jit_precompile beams, packbeam, and that library.
#   ATOMVM_MJS_DIR  directory holding the Emscripten AtomVM.mjs + AtomVM.wasm
#                   (default $ATOMVM_BUILD/../src/platforms/emscripten/build-emsdk/src)
#   OTP_LIB         OTP library directory (default: from erl on PATH)

set -e

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ATOMVM_BUILD="${ATOMVM_BUILD:-$HOME/AtomVM/build.release}"
TARGET=wasm32

# Prefer MacPorts when it is installed, so a local OTP 29 wins over an older
# system erl. CI runners have no /opt/local and use OTP from the default PATH.
if [ -d /opt/local/bin ]; then
    PATH="/opt/local/bin:$PATH"
    export PATH
fi

OTP_LIB="${OTP_LIB:-$(erl -noshell -eval 'io:format("~s",[code:lib_dir()]),halt().')}"
ATOMVM_MJS_DIR="${ATOMVM_MJS_DIR:-$ATOMVM_BUILD/../src/platforms/emscripten/build-emsdk/src}"

OUT="$HERE/_build/node"
AOT="$OUT/aot"
rm -rf "$OUT"
mkdir -p "$AOT" "$OUT/ebin"

JIT_BEAMS="$ATOMVM_BUILD/libs/jit/src/beams"
PACKBEAM="$ATOMVM_BUILD/tools/packbeam/packbeam"
ATOMVMLIB="$ATOMVM_BUILD/libs/atomvmlib-emscripten-$TARGET.avm"

if [ ! -f "$ATOMVMLIB" ]; then
    echo "Missing $ATOMVMLIB." >&2
    echo "Configure the AtomVM build with -DAVM_PRECOMPILED_TARGETS including wasm32" >&2
    echo "and build the atomvmlib-emscripten target." >&2
    exit 1
fi
ATOMVM_MJS="$ATOMVM_MJS_DIR/AtomVM.mjs"
ATOMVM_WASM="$ATOMVM_MJS_DIR/AtomVM.wasm"
if [ ! -f "$ATOMVM_MJS" ] || [ ! -f "$ATOMVM_WASM" ]; then
    echo "Missing AtomVM.mjs/AtomVM.wasm in $ATOMVM_MJS_DIR." >&2
    echo "Build the Emscripten AtomVM (emcmake ... ninja AtomVM) first." >&2
    exit 1
fi

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
    io_lib io_lib_format io_lib_fread io_lib_pretty string unicode_util
    ms_transform}"

cp "$COMPILER_EBIN"/*.beam "$OUT/ebin/"
for m in $STDLIB_MODULES; do
    cp "$STDLIB_EBIN/$m.beam" "$OUT/ebin/"
done

# parse transforms (and their runtime deps) that OTP sources compile with;
# BEAM's erlc finds these in the installed OTP, so bundle them for parity
EUNIT_EBIN=$(ls -d "$OTP_LIB"/eunit-*/ebin | head -1)
SYNTAX_TOOLS_EBIN=$(ls -d "$OTP_LIB"/syntax_tools-*/ebin | head -1)
cp "$EUNIT_EBIN/eunit_autoexport.beam" "$OUT/ebin/"
for m in merl merl_transform erl_syntax erl_syntax_lib erl_comment_scan; do
    cp "$SYNTAX_TOOLS_EBIN/$m.beam" "$OUT/ebin/"
done

# --- 3. AOT precompile (wasm32) ----------------------------------------------
echo "==> jit_precompile $TARGET ($(ls "$OUT/ebin" | wc -l | tr -d ' ') beams)"
erl -pa "$JIT_BEAMS" -noshell -s jit_precompile -s init stop -- \
    "$TARGET" "$AOT/" "$OUT/ebin"/*.beam

# --- 4. pack -----------------------------------------------------------------
echo "==> packbeam"
"$PACKBEAM" create --start atomvm_erlc "$OUT/erlc.avm" \
    "$AOT"/*.beam "$ATOMVMLIB"

# --- 5. stage the Emscripten runtime + launcher ------------------------------
cp "$ATOMVM_MJS" "$OUT/AtomVM.mjs"
cp "$ATOMVM_WASM" "$OUT/AtomVM.wasm"

cat > "$OUT/erlc.mjs" <<'EOF'
#!/usr/bin/env node
//
// Copyright 2026 Paul Guyot <pguyot@kallisys.net>
// SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
//
// Node.js launcher for the AtomVM erlc. Usage:
//   node erlc.mjs [-o dir] [-I dir] [-Dmacro] file.erl ...
//
// AtomVM's Emscripten platform does not forward argv to the Erlang program, so
// the command line is passed through the ATOMVM_ERLC_ARGV environment variable
// (one argument per line), which atomvm_erlc:start/0 reads. The runtime is
// built with -sNODERAWFS, so the real working directory and filesystem are used
// for reading sources and writing .beam files.
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const { default: AtomVM } = await import(join(here, "AtomVM.mjs"));
const args = process.argv.slice(2);

let status = 0;
await AtomVM({
  // The .avm is the only "module" AtomVM loads; the erlc arguments travel via
  // the environment instead.
  arguments: [join(here, "erlc.avm")],
  preRun: [(m) => { m.ENV["ATOMVM_ERLC_ARGV"] = args.join("\n"); }],
  onExit: (code) => { status = code; },
  printErr: (line) => { process.stderr.write(line + "\n"); },
});
process.exit(status);
EOF
chmod +x "$OUT/erlc.mjs"

echo "Built $OUT/erlc.mjs (run: node $OUT/erlc.mjs [options] file.erl)"
