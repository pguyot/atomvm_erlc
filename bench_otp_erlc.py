#!/usr/bin/env python3
# Benchmark AtomVM erlc vs BEAM erlc compiling OTP-29 application source.
#
# For each application (lib/<app>/src and erts/preloaded/src) the same list of
# src/*.erl files is fed to both compilers with the same broad include path
# (every lib/*/include plus erts/include plus the app's own src/include). Each
# compiler is run RUNS times into a fresh temp dir; the median wall time
# (startup included) is reported per app, together with how many .beam files
# each compiler actually produced (success count) so failures are visible.
#
# Identical inputs => the ratio is fair even if some files fail on one or both.
import glob
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OTP = Path(os.environ.get("OTP", "/Users/paul/otp"))
ATOMVM_ERLC = os.environ.get("ATOMVM_ERLC", "/Users/paul/atomvm_erlc/_build/erlc")
BEAM_ERLC = os.environ.get("BEAM_ERLC", "/opt/local/bin/erlc")
RUNS = int(os.environ.get("RUNS", "3"))
TIMEOUT = int(os.environ.get("TIMEOUT", "300"))
ONLY = set(sys.argv[1:])  # optional: restrict to named apps

# Broad include path: every app include dir + erts/include.
BASE_INC = []
for d in sorted(glob.glob(str(OTP / "lib/*/include"))):
    BASE_INC += ["-I", d]
BASE_INC += ["-I", str(OTP / "erts/include")]


def apps():
    out = []
    for srcdir in sorted(glob.glob(str(OTP / "lib/*/src"))):
        out.append((Path(srcdir).parent.name, Path(srcdir)))
    pre = OTP / "erts/preloaded/src"
    if pre.is_dir():
        out.append(("erts_preloaded", pre))
    return out


def run_compiler(erlc, files, inc):
    """Run one batched compile into a fresh dir. Return (seconds, beam_count)."""
    with tempfile.TemporaryDirectory() as out:
        cmd = [erlc, "-o", out] + inc + [str(f) for f in files]
        start = time.perf_counter()
        try:
            subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return float("inf"), -1
        elapsed = time.perf_counter() - start
        beams = len(glob.glob(os.path.join(out, "*.beam")))
        return elapsed, beams


def bench(erlc, files, inc):
    times, beams = [], 0
    for _ in range(RUNS):
        t, b = run_compiler(erlc, files, inc)
        times.append(t)
        beams = b
    return statistics.median(times), beams


def main():
    rows = []
    tot_beam = tot_atom = 0.0
    hdr = f"{'app':<16} {'files':>5} {'beam(s)':>9} {'b_ok':>5} {'atomvm(s)':>10} {'a_ok':>5} {'ratio':>7}"
    print(f"# OTP {(OTP/'OTP_VERSION').read_text().strip()}  RUNS={RUNS}")
    print(f"# beam erlc:   {BEAM_ERLC}")
    print(f"# atomvm erlc: {ATOMVM_ERLC}")
    print(hdr)
    print("-" * len(hdr))
    for app, srcdir in apps():
        if ONLY and app not in ONLY:
            continue
        files = sorted(srcdir.glob("*.erl"))
        if not files:
            continue
        inc = BASE_INC + ["-I", str(srcdir), "-I", str(srcdir.parent / "include")]
        bt, bok = bench(BEAM_ERLC, files, inc)
        at, aok = bench(ATOMVM_ERLC, files, inc)
        ratio = bt / at if at else float("inf")
        tot_beam += bt
        tot_atom += at
        rows.append((app, len(files), bt, bok, at, aok, ratio))
        print(f"{app:<16} {len(files):>5} {bt:>9.3f} {bok:>5} {at:>10.3f} {aok:>5} {ratio:>6.2f}x",
              flush=True)
    print("-" * len(hdr))
    tr = tot_beam / tot_atom if tot_atom else float("inf")
    print(f"{'TOTAL':<16} {'':>5} {tot_beam:>9.3f} {'':>5} {tot_atom:>10.3f} {'':>5} {tr:>6.2f}x")


if __name__ == "__main__":
    main()
