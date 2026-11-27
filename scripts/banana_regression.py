#!/usr/bin/env python3
"""Check and test a Banana checkout with the compiler under development.

Banana, the sibling terminal (`../banana`), is the application that guides
this compiler's library work (docs/internals/roadmap.md). Its own
`tools/test.sh` checks its modules and runs its parser, selection, style
and PTY tests; this script runs that script's `jihoo` commands with the
given compiler instead of Banana's pinned one, from the checkout's root,
and reports each. The commands come from the checkout, so they follow its
tests as they change; other lines (`mkdir`) run as they are.

Nothing needs a display or a graphics driver: the window is built only by
Banana's `tools/build.sh`, which this does not run. The checkout is not
modified beyond what its tests write to its `build/` directory.

    scripts/banana_regression.py --banana ../banana --compiler out/jihoo
"""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def commands(banana):
    """The commands of Banana's tools/test.sh, without the shell's options."""
    script = banana / "tools" / "test.sh"
    out = []
    for line in script.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("set ") or line.startswith("cd "):
            continue
        out.append(shlex.split(line))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--banana", type=Path, default=ROOT.parent / "banana",
                        help="the Banana checkout (default: ../banana)")
    parser.add_argument("--compiler", type=Path, default=ROOT / "out/jihoo")
    args = parser.parse_args()
    banana = args.banana.resolve()
    compiler = args.compiler.resolve()
    if not (banana / "tools" / "test.sh").is_file():
        sys.exit(f"{banana} is not a Banana checkout: it has no tools/test.sh")
    if not compiler.is_file():
        sys.exit(f"no compiler at {compiler}; build it with `jihoo build -O2 compiler/main.jh -o out/jihoo`")
    env = dict(os.environ)
    # The standard library of this checkout, as `jihoo` in the dev shell has.
    env["JIHOO_PATH"] = str(ROOT / "lib")
    revision = subprocess.run(["git", "-C", str(banana), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip() or "unknown"
    print(f"Banana {revision} at {banana}, with {compiler}")
    failed = 0
    for cmd in commands(banana):
        if cmd[0] == "jihoo":
            cmd = [str(compiler)] + cmd[1:]
        shown = " ".join(["jihoo"] + cmd[1:]) if cmd[0] == str(compiler) else " ".join(cmd)
        start = time.monotonic()
        r = subprocess.run(cmd, cwd=banana, env=env, capture_output=True, text=True, timeout=600)
        elapsed = time.monotonic() - start
        if r.returncode == 0:
            print(f"ok    {elapsed:5.1f}s  {shown}")
        else:
            failed += 1
            print(f"FAIL  {elapsed:5.1f}s  {shown}: exit {r.returncode}")
            for line in (r.stdout + r.stderr).splitlines()[-20:]:
                print(f"      {line}")
    if failed:
        print(f"{failed} Banana command(s) failed")
        sys.exit(1)
    print("Banana's checks and tests passed")


if __name__ == "__main__":
    main()
