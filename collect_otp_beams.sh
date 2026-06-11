#!/bin/sh
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Copy the OTP compiler application beams and the stdlib subset that
# AtomVM's atomvmlib does not provide into the rebar3 ebin directory, so
# that the atomvm packbeam/escriptize providers bundle them.
#
# Usage: collect_otp_beams.sh <ebin-dir>

set -e

EBIN="$1"
OTP_LIB="${OTP_LIB:-/opt/local/lib/erlang/lib}"

COMPILER_EBIN=$(ls -d "$OTP_LIB"/compiler-*/ebin | head -1)
STDLIB_EBIN=$(ls -d "$OTP_LIB"/stdlib-*/ebin | head -1)

STDLIB_MODULES="${STDLIB_MODULES:-erl_scan erl_parse erl_lint erl_anno epp
    erl_internal erl_features erl_bits otp_internal erl_eval eval_bits erl_error
    ordsets orddict dict gb_sets gb_trees digraph digraph_utils sofs
    beam_lib filelib graph records erl_expand_records erl_pp rand
    io_lib io_lib_format io_lib_fread io_lib_pretty string unicode_util}"

cp "$COMPILER_EBIN"/*.beam "$EBIN/"
for m in $STDLIB_MODULES; do
    cp "$STDLIB_EBIN/$m.beam" "$EBIN/"
done
