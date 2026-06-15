#!/usr/bin/env python3
# Per-file benchmark: AtomVM erlc vs BEAM erlc on OTP-29 application source.
#
# Each .erl in the given app(s) (default stdlib + crypto) is compiled on its own
# (fresh out dir) RUNS times by each compiler; the median wall time (startup
# included) is reported per file. Only files that BOTH compilers compile
# successfully are timed/reported (others are listed as skipped) so every row is
# a like-for-like comparison.
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
TIMEOUT = int(os.environ.get("TIMEOUT", "120"))
APPS = sys.argv[1:] or ["stdlib", "crypto"]

BASE_INC = []
for d in sorted(glob.glob(str(OTP / "lib/*/include"))):
    BASE_INC += ["-I", d]
BASE_INC += ["-I", str(OTP / "erts/include")]
# The OTP lib root resolves -include_lib("<app>/include/<hdr>.hrl"): epp's
# path_open tries the full "app/include/hdr.hrl" against each -I dir first.
BASE_INC += ["-I", str(OTP / "lib")]


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
    hdr = f"{'file':<26} {'bytes':>7} {'beam(ms)':>9} {'atomvm(ms)':>11} {'ratio':>7}"
    print(f"# OTP {(OTP/'OTP_VERSION').read_text().strip()}  RUNS={RUNS}  apps={','.join(APPS)}")
    print(f"# beam erlc:   {BEAM_ERLC}")
    print(f"# atomvm erlc: {ATOMVM_ERLC}")
    rows, skipped = [], []
    for app in APPS:
        srcdir = OTP / "lib" / app / "src"
        inc = BASE_INC + ["-I", str(srcdir), "-I", str(srcdir.parent / "include")]
        print(f"\n## {app}")
        print(hdr)
        print("-" * len(hdr))
        tb = ta = 0.0
        for src in sorted(srcdir.glob("*.erl")):
            bt, bok = time_one(BEAM_ERLC, src, inc)
            at, aok = time_one(ATOMVM_ERLC, src, inc)
            if not (bok and aok):
                who = ("beam" if not bok else "") + ("/atomvm" if not aok else "")
                skipped.append((f"{app}/{src.name}", who.strip("/")))
                continue
            ratio = bt / at if at else float("inf")
            sz = src.stat().st_size
            tb += bt
            ta += at
            rows.append((app, src.name, sz, bt, at, ratio))
            print(f"{src.name:<26} {sz:>7} {bt*1000:>9.1f} {at*1000:>11.1f} {ratio:>6.2f}x",
                  flush=True)
        if ta:
            print("-" * len(hdr))
            print(f"{'subtotal '+app:<26} {'':>7} {tb*1000:>9.1f} {ta*1000:>11.1f} {tb/ta:>6.2f}x")

    print("\n## skipped (failed to compile on one/both — not comparable)")
    for name, who in skipped:
        print(f"  {name}  [failed: {who}]")

    if rows:
        TB = sum(r[3] for r in rows)
        TA = sum(r[4] for r in rows)
        print(f"\nTOTAL over {len(rows)} common files: "
              f"beam {TB*1000:.1f} ms  atomvm {TA*1000:.1f} ms  -> {TB/TA:.2f}x")
        faster = sum(1 for r in rows if r[5] >= 1.0)
        print(f"AtomVM faster on {faster}/{len(rows)} files; "
              f"slower on {len(rows)-faster}/{len(rows)}")


if __name__ == "__main__":
    main()
