#!/usr/bin/env python3
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Benchmark the AtomVM erlc escript against BEAM's erlc.
#
# For each workload file, each compiler is run N times in a fresh output
# directory; the wall-clock time of the whole invocation (startup included,
# as a user would experience it) is measured and the per-file medians are
# compared. Exits non-zero if the AtomVM erlc is slower in total.
#
# Usage: bench_erlc.py [--runs N] [--atomvm-erlc PATH] [--beam-erlc PATH]
#                      [file.erl ...]

import argparse
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

SMALL = """-module(bench_small).
-export([greet/1, add/2]).

greet(Name) ->
    io:format("Hello, ~s!~n", [Name]).

add(A, B) when is_integer(A), is_integer(B) ->
    A + B.
"""


def make_module(name: str, nfuns: int) -> str:
    # Functions exercising expressions, guards and list comprehensions,
    # sized like typical AtomVM project modules.
    parts = [f"-module({name}).", "-compile(export_all).", ""]
    for i in range(nfuns):
        parts.append(
            f"f{i}(X) when is_integer(X) -> [Y * {i} || Y <- lists:seq(1, X), Y rem 2 =:= 0];\n"
            f"f{i}(X) when is_list(X) -> lists:reverse(X);\n"
            f"f{i}(_) -> undefined."
        )
    return "\n".join(parts) + "\n"


def run_one(cmd, cwd) -> float:
    start = time.perf_counter()
    res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elapsed = time.perf_counter() - start
    if res.returncode != 0:
        raise SystemExit(f"FAILED ({res.returncode}): {' '.join(map(str, cmd))}")
    return elapsed


def bench(compiler_cmd, src: Path, runs: int) -> float:
    times = []
    for _ in range(runs):
        with tempfile.TemporaryDirectory() as out:
            times.append(run_one(compiler_cmd + ["-o", out, str(src)], src.parent))
    return statistics.median(times)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument(
        "--atomvm-erlc", default=str(HERE / "_build" / "default" / "bin" / "erlc")
    )
    ap.add_argument("--beam-erlc", default=shutil.which("erlc"))
    ap.add_argument("files", nargs="*", help="additional .erl files to benchmark")
    args = ap.parse_args()

    if not args.beam_erlc:
        raise SystemExit("BEAM erlc not found; pass --beam-erlc")

    workdir = Path(tempfile.mkdtemp(prefix="bench_erlc_"))
    (workdir / "bench_small.erl").write_text(SMALL)
    (workdir / "bench_gpio.erl").write_text(make_module("bench_gpio", 15))
    (workdir / "bench_handler.erl").write_text(make_module("bench_handler", 40))
    files = [
        workdir / "bench_small.erl",
        workdir / "bench_gpio.erl",
        workdir / "bench_handler.erl",
    ]
    files += [Path(f).resolve() for f in args.files]

    print(f"# runs per file: {args.runs} (median wall time, startup included)")
    print(f"# atomvm erlc: {args.atomvm_erlc}")
    print(f"# beam erlc:   {args.beam_erlc}")
    print(f"{'file':<24} {'beam (s)':>10} {'atomvm (s)':>11} {'ratio':>8}")

    total_beam = 0.0
    total_atomvm = 0.0
    for src in files:
        beam = bench([args.beam_erlc], src, args.runs)
        atomvm = bench([args.atomvm_erlc], src, args.runs)
        total_beam += beam
        total_atomvm += atomvm
        print(f"{src.name:<24} {beam:>10.3f} {atomvm:>11.3f} {beam / atomvm:>7.2f}x")

    print(f"{'TOTAL':<24} {total_beam:>10.3f} {total_atomvm:>11.3f} {total_beam / total_atomvm:>7.2f}x")
    if total_atomvm < total_beam:
        print("atomvm erlc is FASTER than BEAM erlc")
        return 0
    print("atomvm erlc is SLOWER than BEAM erlc")
    return 1


if __name__ == "__main__":
    sys.exit(main())
