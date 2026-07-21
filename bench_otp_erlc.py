#!/usr/bin/env python3
#
# Copyright 2026 Paul Guyot <pguyot@kallisys.net>
# SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
#
# Benchmark the AtomVM erlc escript against BEAM's erlc on the Erlang/OTP
# source tree: every application under lib/<app>/src, plus erts/preloaded/src.
#
# Both compilers get the same file list and the same broad include path (every
# lib/*/include, erts/include, the OTP lib root for -include_lib, and the app's
# own src and include dirs).
#
# Fairness: a file only one compiler can build would otherwise distort the
# ratio -- a compiler that bails out early looks arbitrarily fast. So each app
# is first compiled once by each compiler to discover which .beam files each
# actually produces; only the intersection is timed. Neither front-end aborts
# the batch on a failing file, so that discovery pass sees every file.
#
# Each app is then compiled as a single batched invocation (RUNS times per
# compiler, median wall time reported). Batching is deliberate: it amortises VM
# startup over the app the way a real build does, instead of letting a fixed
# per-invocation startup difference dominate on apps made of many small files.
#
# Usage: bench_otp_erlc.py [--otp DIR] [--runs N] [--atomvm-erlc PATH]
#                          [--beam-erlc PATH] [--timeout S] [app ...]

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


def base_includes(otp: Path):
    inc = []
    for d in sorted(glob.glob(str(otp / "lib/*/include"))):
        inc += ["-I", d]
    inc += ["-I", str(otp / "erts/include")]
    # The OTP lib root resolves -include_lib("<app>/include/<hdr>.hrl"): epp's
    # path_open tries the full "app/include/hdr.hrl" against each -I dir first.
    inc += ["-I", str(otp / "lib")]
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


def common_files(files, beam_erlc, atomvm_erlc, inc, timeout):
    """Files both compilers actually produce a .beam for."""
    _, b_ok = compile_batch(beam_erlc, files, inc, timeout)
    _, a_ok = compile_batch(atomvm_erlc, files, inc, timeout)
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
    print("# only files BOTH compilers compile are timed; 'skip' counts the rest")
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
        if not files:
            continue
        inc = inc_base + ["-I", str(srcdir), "-I", str(srcdir.parent / "include")]

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
