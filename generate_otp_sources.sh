#!/bin/sh
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Generate the stdlib sources that an OTP *source* checkout does not contain.
#
# Two of stdlib's modules are produced during an OTP build and are gitignored,
# so `git clone erlang/otp` gives a lib/stdlib/src that is missing them:
#
#   unicode_util.erl  898 KB, generated from uc_spec/*.txt by
#                     gen_unicode_mod.escript. It is the largest module in the
#                     tree by a wide margin and one of the few where AtomVM
#                     erlc is slower than BEAM erlc, so a benchmark that omits
#                     it reports a better ratio than the real corpus would.
#   erl_parse.erl     951 KB, yecc's output for erl_parse.yrl.
#
# A benchmark run against a checkout therefore measures a different stdlib than
# a run against a built OTP tree, with no warning that it did. This script
# closes that gap; both generators are the ones stdlib's own Makefile invokes
# and together take about two seconds.
#
# Usage: generate_otp_sources.sh OTP_SOURCE_DIR

set -e

OTP="${1:?usage: generate_otp_sources.sh OTP_SOURCE_DIR}"
SRC="$OTP/lib/stdlib/src"

if [ ! -d "$SRC" ]; then
    echo "FAIL: $SRC does not exist, is $OTP an OTP source tree?" >&2
    exit 1
fi

# The escript resolves its inputs as ../uc_spec/*.txt and writes its output to
# the working directory, so it has to run from lib/stdlib/src -- which is
# exactly how stdlib's Makefile calls it.
cd "$SRC"
escript ../uc_spec/gen_unicode_mod.escript
erlc -o . erl_parse.yrl

for f in unicode_util.erl erl_parse.erl; do
    if [ ! -s "$f" ]; then
        echo "FAIL: $f was not generated" >&2
        exit 1
    fi
    echo "generated $(wc -c < "$f" | tr -d ' ') bytes: $SRC/$f"
done
