#!/usr/bin/env python3
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Benchmark the AtomVM erlc escript against BEAM's erlc on the Erlang/OTP
# source tree: every application under lib/<app>/src, plus erts/preloaded/src.
#
# Both compilers get the same file list and the same broad include path (every
# lib/*/include and lib/*/src, erts/include, the OTP lib root for -include_lib,
# and the app's own src and include dirs first), plus the per-application
# version macros that OTP's own Makefiles inject (see APP_MACROS).
#
# Fairness: a file only one compiler can build would otherwise distort the
# ratio, so each app is compiled by each compiler to discover which .beam files
# each actually produces, and only the intersection is timed. Discovery is done
# PER FILE, not from a single batch: BEAM erlc aborts a whole batch at the first
# file that fails to compile, leaving every file after it uncompiled, which
# would make a large part of the corpus look unsupported when only one file
# (often a missing version macro) actually failed. See discover().
#
# The intersection is then compiled as a single batched invocation (RUNS times
# per compiler, median wall time reported). Every file in it compiles on both
# compilers, so no batch aborts. Batching is deliberate: it amortises VM startup
# over the app the way a real build does, instead of letting a fixed
# per-invocation startup difference dominate on apps made of many small files.
#
# --max-bytes caps the corpus by source size. It exists for flavours whose cost
# per module is not merely a constant factor above the native binaries: the
# wasm32 build spends about a second on a 5 KB module and half an hour on a
# 150 KB one, so without a cap a single module decides the whole run. Off by
# default, so the native flavours compile every application in full.
#
# Usage: bench_otp_erlc.py [--otp DIR] [--runs N] [--atomvm-erlc PATH]
#                          [--beam-erlc PATH] [--timeout S] [--max-bytes N]
#                          [app ...]

import argparse
import glob
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--otp", default=os.environ.get("OTP", str(Path.home() / "otp")),
                    help="OTP source tree (default: $OTP or ~/otp)")
    ap.add_argument("--atomvm-erlc", default=os.environ.get("ATOMVM_ERLC"),
                    help="AtomVM erlc binary")
    ap.add_argument("--beam-erlc", default=os.environ.get("BEAM_ERLC") or shutil.which("erlc"),
                    help="BEAM erlc (default: erlc on PATH)")
    ap.add_argument("--runs", type=int, default=int(os.environ.get("RUNS", "3")),
                    help="timed runs per app per compiler (default: 3)")
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("TIMEOUT", "900")),
                    help="per-invocation timeout in seconds (default: 900)")
    ap.add_argument("--max-bytes", type=int, default=int(os.environ.get("MAX_BYTES", "0")),
                    help="skip sources larger than this many bytes (0: no cap)")
    ap.add_argument("apps", nargs="*", help="restrict to these applications")
    args = ap.parse_args()

    if not args.atomvm_erlc:
        default = Path(__file__).resolve().parent / "_build" / "erlc"
        args.atomvm_erlc = str(default)
    if not args.beam_erlc:
        raise SystemExit("BEAM erlc not found; pass --beam-erlc")
    args.otp = Path(args.otp)
    if not (args.otp / "lib").is_dir():
        raise SystemExit(f"no OTP source tree at {args.otp}; pass --otp")
    return args


def beam_emu_flavor(beam_erlc):
    """Whether the baseline BEAM runs the JIT (BeamAsm) or the interpreter.

    Reported because it moves the ratios far more than anything else in the
    baseline: the same erlc is several times faster on a jit build than on an
    emu one, so a comparison is only meaningful next to this line.
    """
    erl = Path(beam_erlc).resolve().parent / "erl"
    erl = str(erl) if erl.is_file() else shutil.which("erl")
    if not erl:
        return "unknown", "erl not found next to erlc or on PATH"
    try:
        res = subprocess.run(
            [erl, "-noshell", "-eval",
             "io:format(\"~p ~s\", [erlang:system_info(emu_flavor),"
             "erlang:system_info(otp_release)]), halt()."],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return "unknown", str(exc)
    out = res.stdout.split()
    if not out:
        return "unknown", (res.stderr.strip() or "no output from erl")
    flavor = out[0]
    release = out[1] if len(out) > 1 else "?"
    note = {
        "jit": "BeamAsm, JIT-compiled",
        "emu": "interpreted, no JIT",
    }.get(flavor, "")
    detail = f"OTP {release}" + (f", {note}" if note else "")
    return flavor, detail


# A few applications reference a version macro that their OTP Makefile injects
# on the erlc command line (e.g. compiler/src/Makefile passes -DCOMPILER_VSN,
# dialyzer uses ?VSN). Without it those files do not compile on ANY erlc, so
# they would be dropped from the corpus for a reason that has nothing to do
# with AtomVM. Inject the same macros, per application, so the file is timed.
# The value is irrelevant to compile time and both compilers get it identically;
# a placeholder is enough. Injected per app rather than globally because some
# unrelated modules (e.g. kernel/group_history) define VSN themselves and a
# global -DVSN would collide ("redefining macro").
APP_MACROS = {
    "compiler": ['-DCOMPILER_VSN="0"'],
    "dialyzer": ['-DVSN="0"'],
}


def base_includes(otp: Path):
    inc = []
    for d in sorted(glob.glob(str(otp / "lib/*/include"))):
        inc += ["-I", d]
    inc += ["-I", str(otp / "erts/include")]
    # The OTP lib root resolves -include_lib("<app>/include/<hdr>.hrl"): epp's
    # path_open tries the full "app/include/hdr.hrl" against each -I dir first.
    inc += ["-I", str(otp / "lib")]
    # Every application's src dir, so private headers one app keeps in src/ and
    # another includes are found (erts/preloaded, for instance, includes
    # kernel/src/inet_int.hrl). The app's own src is placed ahead of this list
    # in main() so its own headers win on any name collision.
    for d in sorted(glob.glob(str(otp / "lib/*/src"))):
        inc += ["-I", d]
    return inc


def apps(otp: Path):
    out = []
    for srcdir in sorted(glob.glob(str(otp / "lib/*/src"))):
        out.append((Path(srcdir).parent.name, Path(srcdir)))
    pre = otp / "erts/preloaded/src"
    if pre.is_dir():
        out.append(("erts_preloaded", pre))
    return out


def compile_batch(erlc, files, inc, timeout):
    """Compile files in one invocation. Return (seconds, {beam basenames})."""
    with tempfile.TemporaryDirectory() as out:
        cmd = [erlc, "-o", out] + inc + [str(f) for f in files]
        start = time.perf_counter()
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            return float("inf"), set()
        elapsed = time.perf_counter() - start
        produced = {Path(p).stem for p in glob.glob(os.path.join(out, "*.beam"))}
        return elapsed, produced


def discover(erlc, files, inc, timeout):
    """Set of file stems this compiler can produce a .beam for.

    A single batch would be a fair discovery only if the compiler compiled the
    whole batch: BEAM erlc ABORTS the entire batch at the first file that fails,
    so every file after the failing one is left uncompiled and would look
    unsupported. (The AtomVM front-end compiles each file in its own process and
    never aborts, so its batch is already complete.) When the batch is short of
    the full set, probe the still-missing files one at a time to tell a real
    failure from a file that merely came after an abort point. With the version
    macros injected most applications compile whole, so the per-file fallback
    only runs for the few files that genuinely fail on a compiler.
    """
    _, produced = compile_batch(erlc, files, inc, timeout)
    if len(produced) == len(files):
        return produced
    for f in files:
        if f.stem not in produced:
            _, one = compile_batch(erlc, [f], inc, timeout)
            produced |= one
    return produced


def common_files(files, beam_erlc, atomvm_erlc, inc, timeout):
    """Files both compilers actually produce a .beam for."""
    b_ok = discover(beam_erlc, files, inc, timeout)
    a_ok = discover(atomvm_erlc, files, inc, timeout)
    both = b_ok & a_ok
    return [f for f in files if f.stem in both], b_ok, a_ok


def median_time(erlc, files, inc, runs, timeout):
    return statistics.median(
        compile_batch(erlc, files, inc, timeout)[0] for _ in range(runs)
    )


def main():
    args = parse_args()
    otp = args.otp
    inc_base = base_includes(otp)
    only = set(args.apps)

    version_file = otp / "OTP_VERSION"
    version = version_file.read_text().strip() if version_file.is_file() else "unknown"
    flavor, detail = beam_emu_flavor(args.beam_erlc)
    print(f"# OTP {version} source at {otp}")
    print(f"# beam erlc:   {args.beam_erlc}")
    print(f"# beam flavor: {flavor} ({detail})")
    print(f"# atomvm erlc: {args.atomvm_erlc}")
    print(f"# runs per app per compiler: {args.runs} (median wall time, batched, startup included)")
    print("# support discovered per file (BEAM erlc aborts a batch at the first error);")
    print("# only files BOTH compilers compile are timed; 'skip' counts the rest")
    if args.max_bytes:
        print(f"# CAPPED CORPUS: sources over {args.max_bytes} bytes are excluded, "
              "so these numbers are not comparable to an uncapped run")
    print()

    hdr = (f"{'application':<18} {'files':>5} {'skip':>4} "
           f"{'beam (s)':>9} {'atomvm (s)':>11} {'ratio':>8}")
    print(hdr)
    print("-" * len(hdr))

    total_beam = total_atomvm = 0.0
    total_files = total_skipped = 0
    rows = []
    atomvm_only_failures = []

    for app, srcdir in apps(otp):
        if only and app not in only:
            continue
        files = sorted(srcdir.glob("*.erl"))
        if args.max_bytes:
            files = [f for f in files if f.stat().st_size <= args.max_bytes]
        if not files:
            continue
        # The app's own src/include first, so its headers win over any
        # same-named header another app keeps in its src dir (inc_base).
        inc = (["-I", str(srcdir), "-I", str(srcdir.parent / "include")]
               + inc_base + APP_MACROS.get(app, []))

        common, b_ok, a_ok = common_files(files, args.beam_erlc, args.atomvm_erlc,
                                          inc, args.timeout)
        skipped = len(files) - len(common)
        for f in files:
            if f.stem in b_ok and f.stem not in a_ok:
                atomvm_only_failures.append(f"{app}/{f.name}")
        if not common:
            print(f"{app:<18} {len(files):>5} {skipped:>4} "
                  f"{'-':>9} {'-':>11} {'-':>8}", flush=True)
            total_skipped += skipped
            continue

        beam = median_time(args.beam_erlc, common, inc, args.runs, args.timeout)
        atomvm = median_time(args.atomvm_erlc, common, inc, args.runs, args.timeout)
        ratio = beam / atomvm if atomvm else float("inf")
        total_beam += beam
        total_atomvm += atomvm
        total_files += len(common)
        total_skipped += skipped
        rows.append((app, ratio))
        print(f"{app:<18} {len(common):>5} {skipped:>4} "
              f"{beam:>9.3f} {atomvm:>11.3f} {ratio:>7.2f}x", flush=True)

    print("-" * len(hdr))
    if not total_atomvm:
        print("no application could be timed")
        return 1
    total_ratio = total_beam / total_atomvm
    print(f"{'TOTAL':<18} {total_files:>5} {total_skipped:>4} "
          f"{total_beam:>9.3f} {total_atomvm:>11.3f} {total_ratio:>7.2f}x")
    print()

    faster = sum(1 for _, r in rows if r >= 1.0)
    print(f"AtomVM erlc is faster on {faster}/{len(rows)} applications")
    if atomvm_only_failures:
        print(f"\n{len(atomvm_only_failures)} file(s) BEAM compiles but AtomVM does not "
              f"(excluded from timings):")
        for name in atomvm_only_failures:
            print(f"  {name}")

    if total_atomvm < total_beam:
        print("\natomvm erlc is FASTER than BEAM erlc")
        return 0
    print("\natomvm erlc is SLOWER than BEAM erlc")
    return 1


if __name__ == "__main__":
    sys.exit(main())
