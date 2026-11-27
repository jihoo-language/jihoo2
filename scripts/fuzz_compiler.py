#!/usr/bin/env python3
"""Feed the compiler broken programs: it must report errors, never crash.

Each case is a file of tests/run, tests/fail, tests/verify or examples with
a few random edits: a number or an operator replaced, which mostly keeps
the program well formed, or a token deleted, doubled or swapped with
another, or the file cut short. The compiler checks it (`check`) or also writes its
LLVM IR (`emit-llvm`, so that cases that still check reach the backend).
Exit status 0 or 1 is right; a trap, a signal, a `panic:` of the compiler
itself or a check that does not end within a minute is a crash, and the
case is kept.

With --valgrind it tests the language's safety instead: the programs of
tests/run without `unsafe`, `extern`, threads, processes or input get only
edits that keep them well formed (numbers and operators), and each one the
compiler accepts is built at -O0 and run under valgrind (which must be on
PATH: `nix-shell -p valgrind`). A program that compiles without `unsafe`
must not touch memory it does not own; one that does is kept. Panics,
traps and nonzero exits are fine.

    scripts/fuzz_compiler.py --compiler out/jihoo                  # 1000 of each
    scripts/fuzz_compiler.py --seed 7 --count 5000 --keep crashes
    nix-shell -p valgrind --run 'scripts/fuzz_compiler.py --valgrind --count 500'
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ["tests/run", "tests/fail", "tests/verify", "examples"]
TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[A-Za-z_][A-Za-z0-9_]*|\d+|\s+|.', re.S)


OPERATORS = "+-*/%&|^<>"
NUMBERS = ["0", "1", "2", "7", "63", "64", "127", "128", "255", "256", "65535", "2147483647",
           "4294967296", "9223372036854775807"]


def mutate(rng, text):
    toks = TOKEN.findall(text)
    for _ in range(rng.randint(1, 6)):
        idx = [i for i, t in enumerate(toks) if not t.isspace()]
        if not idx:
            break
        i = rng.choice(idx)
        r = rng.random()
        # Half the edits keep the program well formed, so that more of
        # them reach the checks and the backend: another number or
        # another operator.
        if r < 0.5:
            nums = [j for j in idx if toks[j].isdigit()]
            ops = [j for j in idx if toks[j] in OPERATORS]
            if r < 0.25 and nums:
                toks[rng.choice(nums)] = rng.choice(NUMBERS)
                continue
            if ops:
                toks[rng.choice(ops)] = rng.choice(OPERATORS)
                continue
        r = rng.random()
        if r < 0.35:
            del toks[i]
        elif r < 0.6:
            toks.insert(i, toks[i])
        elif r < 0.85:
            j = rng.choice(idx)
            toks[i], toks[j] = toks[j], toks[i]
        else:
            toks = toks[:i]
    return "".join(toks)


def well_formed_edits(rng, text):
    """One to three numbers or operators replaced."""
    toks = TOKEN.findall(text)
    for _ in range(rng.randint(1, 3)):
        nums = [j for j, t in enumerate(toks) if t.isdigit()]
        ops = [j for j, t in enumerate(toks) if t in OPERATORS]
        if nums and (rng.random() < 0.5 or not ops):
            toks[rng.choice(nums)] = rng.choice(NUMBERS)
        elif ops:
            toks[rng.choice(ops)] = rng.choice(OPERATORS)
    return "".join(toks)


def safety(args, compiler):
    files = [f for f in sorted((ROOT / "tests/run").glob("*.jh"))
             if not re.search(r"\bunsafe\b|\bextern\b|thread|process|stdin|read_line|uefi",
                              f.read_text())]
    with tempfile.TemporaryDirectory(prefix="jihoo-fuzz-") as directory:
        def case(k):
            rng = random.Random(f"{args.seed}:valgrind:{k}")
            source = rng.choice(files)
            text = well_formed_edits(rng, source.read_text())
            path = Path(directory) / f"safe-{k}.jh"
            exe = Path(directory) / f"safe-{k}"
            path.write_text(text)
            r = subprocess.run([compiler, "build", "-O0", str(path), "-o", str(exe)],
                               capture_output=True, timeout=120)
            if r.returncode != 0:
                return None
            try:
                v = subprocess.run(["valgrind", "-q", "--error-exitcode=99", str(exe)],
                                   capture_output=True, timeout=120)
            except subprocess.TimeoutExpired:
                return True
            if v.returncode != 99:
                return True
            if args.keep:
                args.keep.mkdir(parents=True, exist_ok=True)
                (args.keep / path.name).write_text(text)
            return (f"#{k} from {source.relative_to(ROOT)}: memory error\n" +
                    v.stderr.decode(errors="replace")[-1500:])

        with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
            results = list(pool.map(case, range(args.count)))
    errors = [x for x in results if isinstance(x, str)]
    ran = sum(1 for x in results if x is not None)
    for e in errors:
        print(e)
    print(f"{ran} changed programs compiled and ran under valgrind, {len(errors)} with memory errors")
    sys.exit(1 if errors else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compiler", type=Path, default=ROOT / "out/jihoo")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--count", type=int, default=1000, help="cases per command")
    parser.add_argument("--keep", type=Path, help="directory for the cases that crash")
    parser.add_argument("--valgrind", action="store_true",
                        help="run accepted programs under valgrind instead")
    args = parser.parse_args()
    compiler = str(args.compiler.resolve())
    if args.valgrind:
        return safety(args, compiler)
    files = [f for d in SOURCES for f in sorted((ROOT / d).glob("*.jh"))]
    with tempfile.TemporaryDirectory(prefix="jihoo-fuzz-") as directory:
        def case(job):
            command, k = job
            rng = random.Random(f"{args.seed}:{command}:{k}")
            source = rng.choice(files)
            text = mutate(rng, source.read_text())
            path = Path(directory) / f"{command}-{k}.jh"
            path.write_text(text)
            try:
                r = subprocess.run([compiler, command, str(path)], capture_output=True,
                                   timeout=60)
                err = r.stderr.decode(errors="replace")
                crashed = r.returncode not in (0, 1) or "\npanic:" in "\n" + err
                what = f"exit {r.returncode}"
            except subprocess.TimeoutExpired:
                crashed, err, what = True, "", "no end within 60 s"
            if not crashed:
                return r.returncode == 0
            if args.keep:
                args.keep.mkdir(parents=True, exist_ok=True)
                (args.keep / path.name).write_text(text)
            return f"{command} #{k} from {source.relative_to(ROOT)}: {what}\n{err[-600:]}"

        jobs = [(c, k) for c in ("check", "emit-llvm") for k in range(args.count)]
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
            results = list(pool.map(case, jobs))
    crashes = [x for x in results if isinstance(x, str)]
    accepted = sum(1 for x in results if x is True)
    for c in crashes:
        print(c)
    print(f"{len(jobs) - len(crashes)} of {len(jobs)} changed programs were handled without a crash "
          f"({accepted} still compiled)")
    sys.exit(1 if crashes else 0)


if __name__ == "__main__":
    main()
