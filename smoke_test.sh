#!/bin/sh
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Check that an erlc flavour emits usable BEAM modules.
#
# Two modules are compiled in a SINGLE invocation, on purpose: that is how both
# benchmarks drive the compiler, and a flavour that only works one file at a
# time would otherwise show up as an empty benchmark rather than a failure.
# The result is then loaded and run on BEAM itself, across a module boundary, so
# a subtly wrong .beam cannot pass.
#
# Usage: smoke_test.sh ERLC [args...]
#   ./smoke_test.sh _build/erlc
#   ./smoke_test.sh node _build/node/erlc.mjs

set -e

if [ $# -lt 1 ]; then
    echo "usage: smoke_test.sh ERLC [args...]" >&2
    exit 1
fi

DIR="$(mktemp -d)"
trap 'rm -rf "$DIR"' EXIT

cat > "$DIR/smoke_fact.erl" <<'EOF'
-module(smoke_fact).
-export([fact/1]).
fact(0) -> 1;
fact(N) when N > 0 -> N * fact(N - 1).
EOF

cat > "$DIR/smoke_main.erl" <<'EOF'
-module(smoke_main).
-export([run/0]).
run() -> smoke_fact:fact(10).
EOF

"$@" -o "$DIR" "$DIR/smoke_fact.erl" "$DIR/smoke_main.erl"

for m in smoke_fact smoke_main; do
    if [ ! -f "$DIR/$m.beam" ]; then
        echo "FAIL: $m.beam was not produced" >&2
        exit 1
    fi
done

erl -noshell -pa "$DIR" -eval 'io:format("~p~n", [smoke_main:run()]), halt().' \
    | grep -qx 3628800

echo "OK: emitted valid, correct BEAM modules (batched invocation)"
