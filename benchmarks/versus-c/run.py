#!/usr/bin/env python3
"""Builds each benchmark with jihoo and with clang, checks that the outputs
agree, and prints the best wall-clock time of several runs.

    python3 benchmarks/versus-c/run.py [NAME...]

JIHOO is the compiler (default: out/jihoo, else `jihoo`), CLANG and GCC the
C compilers (default: `clang` and `gcc`; one that is missing is left out)
and RUNS the number of runs (default: 3). clang shares jihoo's backend,
LLVM; GCC optimizes some programs differently (it unrolls `fib`'s
recursion). jihoo builds with
`-O2` (overflow checks on, except those the verifier proves) and with
`-O2 --release` (`+ - *` wrap, as C's do or are undefined); both keep bounds
checks that are not proved. The programs come from the sibling languages
Ruach (fib, sieve, nbody, trees, sort) and Shalem (collatz, matmul), whose
C versions are copied here unchanged except for matmul's output format.
"""
import os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
default = os.path.join(ROOT, "out", "jihoo")
JIHOO = os.environ.get("JIHOO", default if os.path.exists(default) else "jihoo")
CCS = [(n, os.environ.get(n.upper(), n)) for n in ("clang", "gcc")]
CCS = [(n, c) for n, c in CCS if shutil.which(c)]
NAMES = sys.argv[1:] or ["fib", "sieve", "nbody", "trees", "sort", "collatz", "matmul"]
RUNS = int(os.environ.get("RUNS", "3"))


def best(exe):
    times, out = [], None
    for _ in range(RUNS):
        t = time.perf_counter()
        out = subprocess.run([exe], capture_output=True, check=True).stdout
        times.append(time.perf_counter() - t)
    return min(times), out


with tempfile.TemporaryDirectory() as tmp:
    head = f"{'benchmark':10} {'jihoo':>8} {'release':>8}"
    for n, _ in CCS:
        head += f" {n + ' -O2':>9}"
    for n, _ in CCS:
        head += f" {'/' + n:>7}"
    print(head + "   (ratios: release / C)")
    for name in NAMES:
        jh, rel = os.path.join(tmp, name + "-jh"), os.path.join(tmp, name + "-rel")
        src = os.path.join(HERE, name + ".jh")
        subprocess.run([JIHOO, "build", "-O2", src, "-o", jh], check=True)
        subprocess.run([JIHOO, "build", "-O2", "--release", src, "-o", rel], check=True)
        tj, outj = best(jh)
        tr, outr = best(rel)
        if outj != outr:
            sys.exit(f"{name}: the jihoo builds differ:\n{outj.decode()}\n{outr.decode()}")
        times = []
        for n, cc in CCS:
            c = os.path.join(tmp, name + "-" + n)
            subprocess.run([cc, "-O2", os.path.join(HERE, name + ".c"), "-o", c, "-lm"], check=True)
            tc, outc = best(c)
            if outc != outj:
                sys.exit(f"{name}: jihoo and {n} differ:\n{outj.decode()}\n{outc.decode()}")
            times.append(tc)
        line = f"{name:10} {tj:7.2f}s {tr:7.2f}s"
        for t in times:
            line += f" {t:8.2f}s"
        for t in times:
            line += f" {tr / t:7.2f}"
        print(line)
