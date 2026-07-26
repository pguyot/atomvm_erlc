#!/usr/bin/env python3
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Per-file benchmark: AtomVM erlc vs BEAM erlc with ONE compiler invocation per
# .erl file, over a few OTP applications (default: kernel, stdlib, crypto,
# sasl).
#
# This is the counterpart of bench_otp_erlc.py, which batches a whole
# application into a single invocation and so amortises VM startup the way a
# real build does. Here every file pays startup on its own, which is what an
# editor save hook or a one-erlc-per-file Makefile actually does; it also shows
# per file where the two compilers diverge instead of only in the aggregate.
#
# The include paths, the per-application version macros and the "only time
# files BOTH compilers can compile" rule are imported from bench_otp_erlc so
# the two benchmarks cannot drift apart. Support is discovered as a side effect
# here: every file is compiled alone anyway, so a failure is attributed to that
# file and never hides the rest of the corpus.
#
# --atomvm-erlc may be any executable, so the Node.js flavour is benchmarked by
# pointing it at _build/node/erlc.mjs (it carries a `node` shebang).
#
# --max-bytes caps the corpus by source size, as in bench_otp_erlc.py: the
# wasm32 flavour costs about a second on a 5 KB module and half an hour on a
# 150 KB one, so a bounded run has to bound the module size. Off by default.
#
# Usage: bench_otp_perfile.py [--otp DIR] [--runs N] [--atomvm-erlc PATH]
#                             [--beam-erlc PATH] [--timeout S] [--max-bytes N]
#                             [app ...]

import argparse
import os
import shutil
import statistics
import sys
from pathlib import Path

import bench_otp_erlc as batch_bench

DEFAULT_APPS = ["kernel", "stdlib", "crypto", "sasl"]


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--otp", default=os.environ.get("OTP", str(Path.home() / "otp")),
                    help="OTP source tree (default: $OTP or ~/otp)")
    ap.add_argument("--atomvm-erlc", default=os.environ.get("ATOMVM_ERLC"),
                    help="AtomVM erlc binary (or Node.js launcher)")
    ap.add_argument("--beam-erlc", default=os.environ.get("BEAM_ERLC") or shutil.which("erlc"),
                    help="BEAM erlc (default: erlc on PATH)")
    ap.add_argument("--runs", type=int, default=int(os.environ.get("RUNS", "3")),
                    help="timed runs per file per compiler (default: 3)")
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("TIMEOUT", "900")),
                    help="per-invocation timeout in seconds (default: 900)")
    ap.add_argument("--max-bytes", type=int, default=int(os.environ.get("MAX_BYTES", "0")),
                    help="skip sources larger than this many bytes (0: no cap)")
    ap.add_argument("apps", nargs="*", default=[],
                    help=f"applications to benchmark (default: {' '.join(DEFAULT_APPS)})")
    args = ap.parse_args()

    if not args.atomvm_erlc:
        args.atomvm_erlc = str(Path(__file__).resolve().parent / "_build" / "erlc")
    if not args.beam_erlc:
        raise SystemExit("BEAM erlc not found; pass --beam-erlc")
    args.otp = Path(args.otp)
    if not (args.otp / "lib").is_dir():
        raise SystemExit(f"no OTP source tree at {args.otp}; pass --otp")
    if not args.apps:
        args.apps = DEFAULT_APPS
    return args


def time_file(erlc, src, inc, runs, timeout):
    """(median wall seconds, whether a .beam came out) for lone invocations."""
    times = []
    produced_every_time = True
    for _ in range(runs):
        elapsed, produced = batch_bench.compile_batch(erlc, [src], inc, timeout)
        times.append(elapsed)
        produced_every_time = produced_every_time and src.stem in produced
    return statistics.median(times), produced_every_time


def main():
    args = parse_args()
    otp = args.otp
    inc_base = batch_bench.base_includes(otp)
    srcdirs = dict(batch_bench.apps(otp))

    version_file = otp / "OTP_VERSION"
    version = version_file.read_text().strip() if version_file.is_file() else "unknown"
    flavor, detail = batch_bench.beam_emu_flavor(args.beam_erlc)
    print(f"# OTP {version} source at {otp}")
    print(f"# beam erlc:   {args.beam_erlc}")
    print(f"# beam flavor: {flavor} ({detail})")
    print(f"# atomvm erlc: {args.atomvm_erlc}")
    print(f"# applications: {' '.join(args.apps)}")
    print(f"# runs per file per compiler: {args.runs} "
          f"(median wall time, ONE invocation per file, startup included)")
    print("# only files BOTH compilers compile are timed; the rest are listed as skipped")
    if args.max_bytes:
        print(f"# CAPPED CORPUS: sources over {args.max_bytes} bytes are excluded, "
              "so these numbers are not comparable to an uncapped run")

    hdr = (f"{'file':<28} {'bytes':>7} {'beam (ms)':>10} "
           f"{'atomvm (ms)':>12} {'ratio':>8}")
    rows = []
    skipped = []

    for app in args.apps:
        srcdir = srcdirs.get(app)
        if srcdir is None:
            print(f"\n## {app}\n(no such application in {otp})")
            continue
        # The app's own src/include first, so its headers win over any
        # same-named header another app keeps in its src dir (inc_base).
        inc = (["-I", str(srcdir), "-I", str(srcdir.parent / "include")]
               + inc_base + batch_bench.APP_MACROS.get(app, []))

        print(f"\n## {app}")
        print(hdr)
        print("-" * len(hdr))
        app_beam = app_atomvm = 0.0
        app_files = 0
        sources = sorted(srcdir.glob("*.erl"))
        if args.max_bytes:
            sources = [s for s in sources if s.stat().st_size <= args.max_bytes]
        for src in sources:
            beam, beam_ok = time_file(args.beam_erlc, src, inc, args.runs, args.timeout)
            atomvm, atomvm_ok = time_file(args.atomvm_erlc, src, inc, args.runs, args.timeout)
            if not (beam_ok and atomvm_ok):
                who = "beam+atomvm" if not (beam_ok or atomvm_ok) else (
                    "beam" if not beam_ok else "atomvm")
                skipped.append((f"{app}/{src.name}", who))
                continue
            ratio = beam / atomvm if atomvm else float("inf")
            size = src.stat().st_size
            app_beam += beam
            app_atomvm += atomvm
            app_files += 1
            rows.append((app, src.name, beam, atomvm, ratio))
            print(f"{src.name:<28} {size:>7} {beam * 1000:>10.1f} "
                  f"{atomvm * 1000:>12.1f} {ratio:>7.2f}x", flush=True)
        if app_atomvm:
            print("-" * len(hdr))
            print(f"{'subtotal ' + app:<28} {app_files:>7} {app_beam * 1000:>10.1f} "
                  f"{app_atomvm * 1000:>12.1f} {app_beam / app_atomvm:>7.2f}x")

    if skipped:
        print("\n## skipped (one or both compilers produced no .beam)")
        for name, who in skipped:
            print(f"  {name}  [failed: {who}]")

    print()
    if not rows:
        print("no file could be timed")
        return 1

    total_beam = sum(r[2] for r in rows)
    total_atomvm = sum(r[3] for r in rows)
    total_ratio = total_beam / total_atomvm
    print(f"TOTAL over {len(rows)} common files: beam {total_beam * 1000:.1f} ms, "
          f"atomvm {total_atomvm * 1000:.1f} ms -> {total_ratio:.2f}x")

    faster = sum(1 for r in rows if r[4] >= 1.0)
    print(f"AtomVM erlc is faster on {faster}/{len(rows)} files, "
          f"slower on {len(rows) - faster}/{len(rows)}")
    # The per-file median is the number that matters for a save-hook style
    # workload: the total is dominated by whichever handful of files happen to
    # be the largest in the corpus.
    ratios = sorted(r[4] for r in rows)
    print(f"per-file ratio: median {statistics.median(ratios):.2f}x, "
          f"min {ratios[0]:.2f}x, max {ratios[-1]:.2f}x")

    if total_atomvm < total_beam:
        print("\natomvm erlc is FASTER than BEAM erlc")
        return 0
    print("\natomvm erlc is SLOWER than BEAM erlc")
    return 1


if __name__ == "__main__":
    sys.exit(main())
