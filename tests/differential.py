#!/usr/bin/env python3
"""Run random integer programs three ways and compare: in Python, in the
compiler's MIR interpreter (a `const` table) and compiled (at several
optimization levels, with `--release` and with `--audit-proved`).

Each program is a few functions `fK(a: i64, b: i64) -> i64` built from
wrapping arithmetic, bit operations, division by values that are never
zero, shifts, conversions between every integer type, `if`, `match` on
ranges, `for` loops, fixed arrays, a struct with a method, an enum
matched with bindings and guards, `Vec(i64)`, closures that capture,
`String`, generic functions at every integer type, and calls to earlier
functions. Nothing in them overflows a checked operation, so every build
must print the same, and that must be what Python computes.

    tests/differential.py --compiler out/jihoo             # the fixed seeds CI runs
    tests/differential.py --seed 100 --count 500 --keep d  # more, keeping failures in d
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

TYPES = {
    "i8": (8, True), "i16": (16, True), "i32": (32, True), "i64": (64, True),
    "i128": (128, True), "u8": (8, False), "u16": (16, False), "u32": (32, False),
    "u64": (64, False), "u128": (128, False),
}
INPUTS = 6
# The most function bodies one call may run.
CALL_BUDGET = 12


def wrap(v, t):
    bits, signed = TYPES[t]
    v &= (1 << bits) - 1
    if signed and v >= 1 << (bits - 1):
        v -= 1 << bits
    return v


def tdiv(a, b):
    """Division rounding towards zero."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


# ---- expressions: (text, type, evaluate(env)) ----

class Gen:
    def __init__(self, rng):
        self.rng = rng
        self.funcs = 0  # functions defined so far, which calls may use
        # Inside a loop calls are left out, which keeps the interpreter's
        # steps well under its limit.
        self.loops = 0
        # Per function, how many function bodies a call of it runs, and the
        # same for the function being written; calls stay within a budget.
        self.costs = []
        self.cost = 1

    def literal(self, t):
        bits, signed = TYPES[t]
        r = self.rng.random()
        if r < 0.3:
            v = self.rng.randint(0, 9)
        elif r < 0.6:
            v = (1 << min(bits, 63) - 1) - self.rng.randint(0, 2) if bits > 1 else 1
        else:
            v = self.rng.getrandbits(min(bits, 63) - (1 if signed else 0))
        # A literal is at most 64 bits and never negative here.
        v = max(0, min(v, (1 << min(bits - (1 if signed else 0), 63)) - 1))
        return (f"({v} as {t})", t, lambda env, v=v: v)

    def var(self, env_types, t):
        names = [n for n, nt in env_types.items() if nt == t]
        return self.rng.choice(names) if names else None

    def expr(self, env_types, t, depth):
        rng = self.rng
        if depth <= 0 or rng.random() < 0.2:
            n = self.var(env_types, t)
            if n is not None and rng.random() < 0.7:
                return (n, t, lambda env, n=n: env[n])
            if rng.random() < 0.5:
                # a conversion from a variable of another type
                n = rng.choice(list(env_types))
                return self.convert((n, env_types[n], lambda env, n=n: env[n]), t)
            return self.literal(t)
        kind = rng.choice(["arith", "arith", "bits", "shift", "div", "conv", "if", "match",
                           "call", "pick"])
        bits, signed = TYPES[t]
        if kind == "arith":
            op = rng.choice(["+%", "-%", "*%"])
            a = self.expr(env_types, t, depth - 1)
            b = self.expr(env_types, t, depth - 1)
            f = {"+%": lambda x, y: x + y, "-%": lambda x, y: x - y,
                 "*%": lambda x, y: x * y}[op]
            return (f"({a[0]} {op} {b[0]})", t,
                    lambda env, a=a, b=b, f=f: wrap(f(a[2](env), b[2](env)), t))
        if kind == "bits":
            op = rng.choice(["&", "|", "^"])
            a = self.expr(env_types, t, depth - 1)
            b = self.expr(env_types, t, depth - 1)
            f = {"&": lambda x, y: x & y, "|": lambda x, y: x | y,
                 "^": lambda x, y: x ^ y}[op]
            return (f"({a[0]} {op} {b[0]})", t,
                    lambda env, a=a, b=b, f=f: wrap(f(a[2](env), b[2](env)), t))
        if kind == "shift":
            op = rng.choice(["<<", ">>"])
            a = self.expr(env_types, t, depth - 1)
            b = self.expr(env_types, t, depth - 1)

            def shift(env, a=a, b=b, op=op):
                x = a[2](env)
                # the amount modulo the width, of the amount's bits
                n = (b[2](env) & ((1 << bits) - 1)) % bits
                return wrap(x << n, t) if op == "<<" else wrap(x >> n, t)
            return (f"({a[0]} {op} {b[0]})", t, shift)
        if kind == "div":
            op = rng.choice(["/", "%"])
            a = self.expr(env_types, t, depth - 1)
            b = self.expr(env_types, t, depth - 1)
            if signed and rng.random() < 0.5:
                # odd, so never zero, and sometimes -1 (`MIN / -1` wraps)
                d_text = f"({b[0]} | (1 as {t}))"
                d = lambda env, b=b: b[2](env) | 1
            else:
                d_text = f"(({b[0]} & (7 as {t})) +% (1 as {t}))"
                d = lambda env, b=b: (b[2](env) & 7) + 1

            def div(env, a=a, d=d, op=op):
                x, y = a[2](env), d(env)
                q = tdiv(x, y)
                return wrap(q, t) if op == "/" else wrap(x - q * y, t)
            return (f"({a[0]} {op} {d_text})", t, div)
        if kind == "conv":
            s = rng.choice(list(TYPES))
            return self.convert(self.expr(env_types, s, depth - 1), t)
        if kind == "if":
            s = rng.choice(list(TYPES))
            op = rng.choice(["<", "<=", "==", "!=", ">", ">="])
            x = self.expr(env_types, s, depth - 1)
            y = self.expr(env_types, s, depth - 1)
            a = self.expr(env_types, t, depth - 1)
            b = self.expr(env_types, t, depth - 1)
            f = {"<": lambda p, q: p < q, "<=": lambda p, q: p <= q,
                 "==": lambda p, q: p == q, "!=": lambda p, q: p != q,
                 ">": lambda p, q: p > q, ">=": lambda p, q: p >= q}[op]

            def choose(env, x=x, y=y, a=a, b=b, f=f):
                return a[2](env) if f(x[2](env), y[2](env)) else b[2](env)
            return (f"(if {x[0]} {op} {y[0]} {{ {a[0]} }} else {{ {b[0]} }})", t, choose)
        if kind == "match":
            x = self.expr(env_types, t, depth - 1)
            arms = [self.expr(env_types, t, depth - 1) for _ in range(3)]

            def pick(env, x=x, arms=arms):
                k = x[2](env) & 7
                return arms[0][2](env) if k == 0 else arms[1][2](env) if k <= 3 else arms[2][2](env)
            return (f"(match {x[0]} & (7 as {t}) {{ 0 => {arms[0][0]}, 1..=3 => {arms[1][0]}, "
                    f"_ => {arms[2][0]} }})", t, pick)
        if kind == "pick":
            # the generic `pick(T, ...)`, at this type
            s = rng.choice(list(TYPES))
            x = self.expr(env_types, s, depth - 1)
            y = self.expr(env_types, s, depth - 1)
            a = self.expr(env_types, t, depth - 1)
            b = self.expr(env_types, t, depth - 1)

            def pick(env, x=x, y=y, a=a, b=b):
                return a[2](env) if x[2](env) <= y[2](env) else b[2](env)
            return (f"pick({x[0]} <= {y[0]}, {a[0]}, {b[0]})", t, pick)
        # a call of an earlier function
        if self.funcs == 0 or self.loops > 0:
            return self.literal(t)
        k = rng.randrange(self.funcs)
        if self.cost + self.costs[k] > CALL_BUDGET:
            return self.literal(t)
        self.cost += self.costs[k]
        a = self.convert(self.expr(env_types, rng.choice(list(TYPES)), depth - 1), "i64")
        b = self.convert(self.expr(env_types, rng.choice(list(TYPES)), depth - 1), "i64")

        def call(env, k=k, a=a, b=b):
            return env["#funcs"][k](a[2](env), b[2](env), env["#funcs"])
        return self.convert((f"f{k}({a[0]}, {b[0]})", "i64", call), t)

    def convert(self, e, t):
        if e[1] == t:
            return e
        return (f"({e[0]} as {t})", t, lambda env, e=e: wrap(e[2](env), t))

    # ---- statements: (lines, run(env)) ----

    def block(self, env_types, depth, count, indent):
        lines, runs = [], []
        for _ in range(count):
            l, r = self.stmt(env_types, depth, indent)
            lines += l
            runs.append(r)

        def run(env, runs=runs):
            for r in runs:
                r(env)
        return lines, run

    def stmt(self, env_types, depth, indent):
        rng = self.rng
        pad = "    " * indent
        kind = rng.choice(["var", "var", "set", "set", "for", "if", "array", "struct",
                           "enum", "vec", "closure", "slice", "string"]) if depth > 0 else rng.choice(["var", "set"])
        if kind == "var" or (kind == "set" and not [n for n in env_types if n[0] == "v"]):
            t = rng.choice(list(TYPES))
            e = self.expr(env_types, t, 3)
            n = f"v{len(env_types)}"
            env_types[n] = t

            def run(env, n=n, e=e):
                env[n] = e[2](env)
            return [f"{pad}var {n}: {t} = {e[0]}"], run
        if kind == "set":
            n = rng.choice([n for n in env_types if n[0] == "v"])
            e = self.expr(env_types, env_types[n], 3)

            def run(env, n=n, e=e):
                env[n] = e[2](env)
            return [f"{pad}{n} = {e[0]}"], run
        if kind == "for":
            k = rng.randint(1, 6)
            i = f"i{len(env_types)}"
            inner = dict(env_types)
            inner[i] = "i64"
            self.loops += 1
            lines, body = self.block(inner, depth - 1, rng.randint(1, 3), indent + 1)
            self.loops -= 1

            def run(env, i=i, body=body, k=k):
                saved = dict(env)
                for x in range(k):
                    env[i] = x
                    body(env)
                for n in list(env):
                    if n not in saved:
                        del env[n]
            return [f"{pad}for {i} in 0..{k} {{"] + lines + [f"{pad}}}"], run
        if kind == "if":
            s = rng.choice(list(TYPES))
            x = self.expr(env_types, s, 2)
            y = self.expr(env_types, s, 2)
            then_lines, then_run = self.block(dict(env_types), depth - 1, rng.randint(1, 2),
                                              indent + 1)
            else_lines, else_run = self.block(dict(env_types), depth - 1, rng.randint(1, 2),
                                              indent + 1)

            def run(env, x=x, y=y, a=then_run, b=else_run):
                saved = set(env)
                (a if x[2](env) < y[2](env) else b)(env)
                for n in list(env):
                    if n not in saved:
                        del env[n]
            return ([f"{pad}if {x[0]} < {y[0]} {{"] + then_lines + [f"{pad}}} else {{"] +
                    else_lines + [f"{pad}}}"]), run
        if kind == "struct":
            return self.struct(env_types, pad)
        if kind == "enum":
            return self.enum(env_types, pad)
        if kind == "vec":
            return self.vec(env_types, pad)
        if kind == "closure":
            return self.closure(env_types, pad)
        if kind == "slice":
            return self.slice(env_types, pad)
        if kind == "string":
            return self.string(env_types, pad)
        # an array: built, one element changed, one read into a variable
        n = f"v{len(env_types)}"
        elems = [self.expr(env_types, "i64", 2) for _ in range(4)]
        at = self.convert(self.expr(env_types, rng.choice(list(TYPES)), 2), "i64")
        value = self.expr(env_types, "i64", 2)
        read = self.convert(self.expr(env_types, rng.choice(list(TYPES)), 2), "i64")
        env_types[n] = "i64"
        arr = f"a{n}"

        def run(env, n=n, elems=elems, at=at, value=value, read=read):
            a = [e[2](env) for e in elems]
            a[at[2](env) & 3] = value[2](env)
            env[n] = a[read[2](env) & 3]
        return [
            f"{pad}var {arr}: [i64; 4] = [{', '.join(e[0] for e in elems)}]",
            f"{pad}{arr}[{at[0]} & 3] = {value[0]}",
            f"{pad}var {n}: i64 = {arr}[{read[0]} & 3]",
        ], run

    def struct(self, env_types, pad):
        """A `P` built, one field changed, and read into a variable directly
        and through `P.mix(self)` or `mix(p: P)`, which borrow it."""
        rng = self.rng
        n = f"v{len(env_types)}"
        p = f"s{n}"
        x = self.expr(env_types, "i64", 2)
        y = self.expr(env_types, "i32", 2)
        field = rng.choice(["x", "y"])
        value = self.expr(env_types, "i64" if field == "x" else "i32", 2)
        read = rng.choice(["mix", "method", "x", "y"])
        env_types[n] = "i64"

        def run(env, n=n, x=x, y=y, field=field, value=value, read=read):
            s = {"x": x[2](env), "y": y[2](env)}
            s[field] = value[2](env)
            if read == "x":
                env[n] = s["x"]
            elif read == "y":
                env[n] = s["y"]
            elif read == "mix":
                env[n] = s["x"] ^ s["y"]
            else:
                env[n] = wrap(s["x"] * 3 + s["y"], "i64")
        text = {"x": f"{p}.x", "y": f"({p}.y as i64)", "mix": f"mix({p})",
                "method": f"{p}.scaled()"}[read]
        return [
            f"{pad}var {p} = P {{ x: {x[0]}, y: {y[0]} }}",
            f"{pad}{p}.{field} = {value[0]}",
            f"{pad}var {n}: i64 = {text}",
        ], run

    def enum(self, env_types, pad):
        """An `E` chosen by `match`, then matched with bindings and a guard."""
        rng = self.rng
        n = f"v{len(env_types)}"
        e = f"e{n}"
        k = self.expr(env_types, rng.choice(list(TYPES)), 2)
        a = self.expr(env_types, "i64", 2)
        q = self.expr(env_types, "u8", 2)
        r = self.expr(env_types, "i64", 2)
        c = self.expr(env_types, "i64", 2)
        g = rng.randint(0, 255)
        env_types[n] = "i64"

        def run(env, n=n, k=k, a=a, q=q, r=r, c=c, g=g):
            sel = k[2](env) & 3
            if sel == 0:
                v = ("A", a[2](env))
            elif sel == 1:
                v = ("B", q[2](env), r[2](env))
            else:
                v = ("C",)
            if v[0] == "A":
                env[n] = v[1]
            elif v[0] == "B" and v[1] > g:
                env[n] = wrap(v[2] - v[1], "i64")
            elif v[0] == "B":
                env[n] = v[1] ^ v[2]
            else:
                env[n] = c[2](env)
        kt = k[1]
        return [
            f"{pad}var {e}: E = match {k[0]} & (3 as {kt}) {{ 0 => E.A({a[0]}), "
            f"1 => E.B({q[0]}, {r[0]}), _ => E.C }}",
            f"{pad}var {n}: i64 = match {e} {{ A(x) => x, B(x, y) if x > {g} => y -% (x as i64), "
            f"B(x, y) => (x as i64) ^ y, C => {c[0]} }}",
        ], run

    def vec(self, env_types, pad):
        """A `Vec(i64)` pushed in a loop, one element changed, then read by
        index, by length or by a `for` over it."""
        rng = self.rng
        n = f"v{len(env_types)}"
        w, j = f"w{n}", f"j{n}"
        count = rng.randint(1, 6)
        inner = dict(env_types)
        inner[j] = "i64"
        self.loops += 1
        elem = self.expr(inner, "i64", 2)
        self.loops -= 1
        at = self.convert(self.expr(env_types, rng.choice(list(TYPES)), 2), "u64")
        value = self.expr(env_types, "i64", 2)
        read = rng.choice(["index", "len", "fold"])
        env_types[n] = "i64"

        def run(env, n=n, j=j, count=count, elem=elem, at=at, value=value, read=read):
            v = []
            for x in range(count):
                inner = dict(env)
                inner[j] = x
                v.append(wrap(elem[2](inner) + x, "i64"))
            i = at[2](env) % count
            v[i] = value[2](env)
            if read == "index":
                env[n] = v[(i + 1) % count]
            elif read == "len":
                env[n] = len(v)
            else:
                t = 0
                for x in v:
                    t = wrap(t * 31 + x, "i64")
                env[n] = t
        i = f"(({at[0]}) % {count})"
        reads = {
            "index": [f"{pad}var {n}: i64 = {w}[(({i} + 1) % {count}) as i64]"],
            "len": [f"{pad}var {n}: i64 = {w}.len()"],
            "fold": [f"{pad}var {n}: i64 = 0",
                     f"{pad}for x in {w} {{ {n} = {n} *% 31 +% x }}"],
        }[read]
        return [
            f"{pad}var {w} = Vec(i64).new()",
            f"{pad}for {j} in 0..{count} {{ {w}.push({elem[0]} +% {j}) }}",
            f"{pad}{w}[{i} as i64] = {value[0]}",
        ] + reads, run

    def closure(self, env_types, pad):
        """A closure of one parameter that captures the variables in scope,
        called twice."""
        rng = self.rng
        n = f"v{len(env_types)}"
        c, x = f"c{n}", f"x{n}"
        inner = dict(env_types)
        inner[x] = "i64"
        # Called twice, so without calls, as in a loop.
        self.loops += 1
        body = self.expr(inner, "i64", 2)
        self.loops -= 1
        a = self.expr(env_types, "i64", 2)
        b = self.expr(env_types, "i64", 2)
        env_types[n] = "i64"

        def run(env, n=n, x=x, body=body, a=a, b=b):
            def call(v):
                inner = dict(env)
                inner[x] = v
                return body[2](inner)
            env[n] = wrap(call(a[2](env)) - call(b[2](env)), "i64")
        return [
            f"{pad}let {c} = fn({x}: i64) -> i64 {{ {body[0]} }}",
            f"{pad}var {n}: i64 = {c}({a[0]}) -% {c}({b[0]})",
        ], run

    def slice(self, env_types, pad):
        """An array of any integer type summed by the generic `total(T, [T])`."""
        rng = self.rng
        n = f"v{len(env_types)}"
        arr = f"a{n}"
        t = rng.choice(list(TYPES))
        count = rng.randint(1, 4)
        elems = [self.expr(env_types, t, 2) for _ in range(count)]
        lo = rng.randint(0, count)
        env_types[n] = "i64"

        def run(env, n=n, elems=elems, lo=lo):
            total = 0
            for e in elems[lo:]:
                total = wrap(total + wrap(e[2](env), "i64"), "i64")
            env[n] = total
        return [
            f"{pad}var {arr}: [{t}; {count}] = [{', '.join(e[0] for e in elems)}]",
            f"{pad}var {n}: i64 = total({arr}[{lo}..{count}])",
        ], run

    def string(self, env_types, pad):
        """A `String` grown by `push` in a loop and `push_str`, read by
        length or by a byte."""
        rng = self.rng
        n = f"v{len(env_types)}"
        st, j = f"s{n}", f"j{n}"
        start = "".join(rng.choice("abc") for _ in range(rng.randint(0, 3)))
        end = "".join(rng.choice("xyz") for _ in range(rng.randint(0, 3)))
        k = self.convert(self.expr(env_types, rng.choice(list(TYPES)), 2), "i64")
        at = self.convert(self.expr(env_types, rng.choice(list(TYPES)), 2), "u64")
        read = rng.choice(["len", "byte"])
        env_types[n] = "i64"

        def run(env, n=n, k=k, at=at, read=read):
            text = start + "q" * (k[2](env) & 3) + end + "!"
            if read == "len":
                env[n] = len(text)
            else:
                env[n] = ord(text[at[2](env) % len(text)])
        lines = [
            f'{pad}var {st} = String.from("{start}")',
            f"{pad}for {j} in 0..({k[0]} & 3) {{ {st}.push('q') }}",
            f'{pad}{st}.push_str("{end}!")',
        ]
        if read == "len":
            lines.append(f"{pad}var {n}: i64 = {st}.len()")
        else:
            lines.append(f"{pad}var {n}: i64 = {st}.as_str()[(({at[0]}) % ({st}.len() as u64)) as i64] as i64")
        return lines, run

    def function(self):
        self.cost = 1
        env_types = {"a": "i64", "b": "i64"}
        lines, body = self.block(env_types, 2, self.rng.randint(2, 6), 1)
        result = self.expr(env_types, "i64", 3)
        k = self.funcs
        self.funcs += 1
        self.costs.append(self.cost)
        text = "\n".join([f"fn f{k}(a: i64, b: i64) -> i64 {{"] + lines +
                         [f"    {result[0]}", "}"])

        def call(a, b, funcs, body=body, result=result):
            env = {"a": a, "b": b, "#funcs": funcs}
            body(env)
            return result[2](env)
        return text, call


def literal_i64(v):
    """`v` as jihoo source: literals are never negative."""
    if v >= 0:
        return str(v)
    return f"(0 -% {-(v + 1)} -% 1)"


# The types the statements use.
PRELUDE = """struct P {
    x: i64
    y: i32
}

impl P {
    fn scaled(self) -> i64 { self.x *% 3 +% (self.y as i64) }
}

fn mix(p: P) -> i64 { p.x ^ (p.y as i64) }

fn pick(comptime T: type, c: bool, x: T, y: T) -> T { if c { x } else { y } }

fn total(comptime T: type, xs: [T]) -> i64 {
    var t: i64 = 0
    for x in xs { t = t +% (x as i64) }
    t
}

enum E {
    A(i64)
    B(u8, i64)
    C
}
"""


def program(seed, functions):
    rng = random.Random(seed)
    g = Gen(rng)
    texts, calls = [], []
    for _ in range(functions):
        t, c = g.function()
        texts.append(t)
        calls.append(c)
    edges = [0, 1, -1, 2**63 - 1, -2**63, 255, -128, 2**31]
    inputs = [(rng.choice(edges) if rng.random() < 0.3 else wrap(rng.getrandbits(64), "i64"),
               rng.choice(edges) if rng.random() < 0.3 else rng.randint(-100, 100))
              for _ in range(INPUTS)]
    expected = []
    for k, c in enumerate(calls):
        for a, b in inputs:
            expected.append(c(a, b, calls))
    xs = ", ".join(literal_i64(a) for a, _ in inputs)
    ys = ", ".join(literal_i64(b) for _, b in inputs)
    out = ["import std.env", "", PRELUDE] + texts + [""]
    for k in range(functions):
        out.append(f"fn table{k}() -> [i64; {INPUTS}] {{")
        out.append(f"    let xs: [i64; {INPUTS}] = [{xs}]")
        out.append(f"    let ys: [i64; {INPUTS}] = [{ys}]")
        out.append(f"    var t = [0; {INPUTS}]")
        out.append(f"    for j in 0..{INPUTS} {{ t[j] = f{k}(xs[j], ys[j]) }}")
        out.append("    t")
        out.append("}")
        out.append(f"const T{k} = table{k}()")
    out += [
        "",
        "fn main() {",
        "    // 0 however many arguments there are, unknown while compiling",
        "    let z = env.args().len() >> 40",
        f"    let xs: [i64; {INPUTS}] = [{xs}]",
        f"    let ys: [i64; {INPUTS}] = [{ys}]",
        f"    for j in 0..{INPUTS} {{",
    ]
    for k in range(functions):
        out.append(f"        println(f{k}(xs[j] +% z, ys[j] +% z))")
        out.append(f"        println(T{k}[j])")
    out += ["    }", "}", ""]
    # The output: per input, per function, the compiled and the interpreted result.
    lines = []
    for j in range(INPUTS):
        for k in range(functions):
            v = expected[k * INPUTS + j]
            lines += [str(v), str(v)]
    return "\n".join(out), lines


BUILDS = [["-O0"], ["-O2"], ["-O2", "--release"], ["-O1", "--audit-proved"]]


def check(compiler, seed, functions, directory, keep):
    source, expected = program(seed, functions)
    path = Path(directory) / f"diff{seed}.jh"
    path.write_text(source)
    problems = []
    for flags in BUILDS:
        r = subprocess.run([str(compiler), "run", *flags, str(path)], capture_output=True,
                           text=True, timeout=600)
        what = f"seed {seed} {' '.join(flags)}"
        if r.returncode != 0:
            problems.append(f"{what}: exit {r.returncode}\n{r.stderr[-2000:]}")
            continue
        got = r.stdout.splitlines()
        for i, (g, e) in enumerate(zip(got, expected)):
            if g != e:
                j, rest = divmod(i, 2 * functions)
                k, interpreted = divmod(rest, 2)
                where = "the interpreter" if interpreted else "compiled code"
                problems.append(f"{what}: f{k} on input {j}: {where} gives {g}, Python {e}")
                break
        else:
            if len(got) != len(expected):
                problems.append(f"{what}: {len(got)} lines, expected {len(expected)}")
    if problems and keep:
        keep.mkdir(parents=True, exist_ok=True)
        (keep / path.name).write_text(source)
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compiler", type=Path, default=ROOT / "out/jihoo")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--functions", type=int, default=4)
    parser.add_argument("--keep", type=Path, help="directory for the programs that fail")
    parser.add_argument("--print", action="store_true", help="print the first program and stop")
    args = parser.parse_args()
    if args.print:
        print(program(args.seed, args.functions)[0])
        return
    compiler = args.compiler.resolve()
    failed = 0
    # Programs are checked in parallel, and reported in order of seed.
    with tempfile.TemporaryDirectory(prefix="jihoo-differential-") as directory, \
            ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
        results = pool.map(lambda seed: check(compiler, seed, args.functions, directory,
                                              args.keep),
                           range(args.seed, args.seed + args.count))
        for problems in results:
            for p in problems:
                print(p)
            failed += bool(problems)
    print(f"{args.count - failed} of {args.count} programs agree")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
