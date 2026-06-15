#!/usr/bin/env python3
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Benchmark the AtomVM erlc across the full build matrix and against BEAM erlc,
# compiling real OTP-29 application source (stdlib, kernel, sasl, crypto).
#
# Compilers compared:
#   BEAM        /opt/local/bin/erlc                         (reference)
#   emu noSMP   _build/matrix/erlc-emu-nosmp   (interpreter, no SMP)
#   emu SMP     _build/matrix/erlc-emu-smp     (interpreter, SMP)
#   JIT noSMP   _build/matrix/erlc-jit-nosmp   (AOT native, no SMP)
#   JIT SMP     _build/matrix/erlc-jit-smp     (AOT native, SMP)
#
# Methodology:
#   * Each .erl in each app is compiled on its own, in a fresh temp out dir,
#     RUNS times by every compiler; the per-file median wall time (whole
#     process, VM startup included -- what a user feels) is the datum.
#   * A file is only counted if EVERY compiler produces a .beam (a like-for-like
#     comparison). AtomVM lacks some bitstring features, so files that fail to
#     compile on AtomVM are listed as skipped and excluded from all totals.
#   * Per compiler we report the SUM over the common files and the MEAN per
#     file, and the speed-up vs BEAM (sum_beam / sum_config).
#
# Env overrides: OTP (default /Users/paul/otp), BEAM_ERLC, MATRIX_DIR, RUNS,
#                TIMEOUT, APPS (space separated, default "stdlib kernel sasl crypto")

import glob
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OTP = Path(os.environ.get("OTP", "/Users/paul/otp"))
BEAM_ERLC = os.environ.get("BEAM_ERLC", "/opt/local/bin/erlc")
MATRIX = Path(os.environ.get("MATRIX_DIR", HERE / "_build" / "matrix"))
RUNS = int(os.environ.get("RUNS", "3"))
TIMEOUT = int(os.environ.get("TIMEOUT", "120"))
APPS = os.environ.get("APPS", "stdlib kernel sasl crypto").split()

# (label, executable) -- BEAM first as the reference column.
COMPILERS = [
    ("BEAM", BEAM_ERLC),
    ("emu noSMP", str(MATRIX / "erlc-emu-nosmp")),
    ("emu SMP", str(MATRIX / "erlc-emu-smp")),
    ("JIT noSMP", str(MATRIX / "erlc-jit-nosmp")),
    ("JIT SMP", str(MATRIX / "erlc-jit-smp")),
]

# Include path so epp resolves -include / -include_lib across all of OTP.
BASE_INC = []
for d in sorted(glob.glob(str(OTP / "lib/*/include"))):
    BASE_INC += ["-I", d]
BASE_INC += ["-I", str(OTP / "erts/include"), "-I", str(OTP / "lib")]


def time_one(erlc, src, inc):
    """median wall time over RUNS, and whether a .beam was produced."""
    times, ok = [], False
    for _ in range(RUNS):
        with tempfile.TemporaryDirectory() as out:
            cmd = [erlc, "-o", out, *inc, str(src)]
            start = time.perf_counter()
            try:
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                return float("inf"), False
            times.append(time.perf_counter() - start)
            ok = bool(glob.glob(os.path.join(out, "*.beam")))
    return statistics.median(times), ok


def main():
    for label, exe in COMPILERS:
        if not Path(exe).exists() and not (label == "BEAM"):
            raise SystemExit(f"missing compiler {label}: {exe}\n"
                             "  build with build_erlc_variant.sh first")

    print(f"# OTP {(OTP/'OTP_VERSION').read_text().strip()}  RUNS={RUNS}  "
          f"apps={','.join(APPS)}")
    for label, exe in COMPILERS:
        print(f"#   {label:<10} {exe}")

    labels = [c[0] for c in COMPILERS]
    # per-compiler accumulated sum of per-file medians over common files
    totals = {lab: 0.0 for lab in labels}
    ncommon = 0
    skipped = []

    colw = 11
    hdr = f"{'file':<26}{'bytes':>8}" + "".join(l.rjust(colw) for l in labels)

    for app in APPS:
        srcdir = OTP / "lib" / app / "src"
        inc = BASE_INC + ["-I", str(srcdir), "-I", str(srcdir.parent / "include")]
        print(f"\n## {app}")
        print(hdr)
        print("-" * len(hdr))
        for src in sorted(srcdir.glob("*.erl")):
            res = {}
            failed = []
            for lab, exe in COMPILERS:
                t, ok = time_one(exe, src, inc)
                res[lab] = t
                if not ok:
                    failed.append(lab)
            if failed:
                skipped.append((f"{app}/{src.name}", ",".join(failed)))
                continue
            ncommon += 1
            for lab in labels:
                totals[lab] += res[lab]
            sz = src.stat().st_size
            cells = "".join(f"{res[l]*1000:>{colw}.1f}" for l in labels)
            print(f"{src.name:<26}{sz:>8}{cells}", flush=True)

    print("\n" + "=" * len(hdr))
    print(f"Common files compiled by ALL: {ncommon}")
    print(f"\n{'compiler':<12}{'sum (ms)':>12}{'mean/file (ms)':>16}{'vs BEAM':>10}")
    print("-" * 50)
    base = totals["BEAM"] or float("nan")
    for lab in labels:
        s = totals[lab] * 1000.0
        mean = s / ncommon if ncommon else float("nan")
        ratio = base / totals[lab] if totals[lab] else float("nan")
        print(f"{lab:<12}{s:>12.1f}{mean:>16.2f}{ratio:>9.2f}x")

    print(f"\n## skipped ({len(skipped)} files: AtomVM/BEAM could not compile "
          "-- excluded from all totals)")
    for name, who in skipped:
        print(f"  {name:<34} [failed: {who}]")


if __name__ == "__main__":
    main()
