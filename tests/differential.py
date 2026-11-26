#!/usr/bin/env python3
"""Run random programs three ways and compare: in Python, in the
compiler's MIR interpreter (a `const` table) and compiled (at several
optimization levels, with `--release` and with `--audit-proved`).

Each program is a few functions `fK(a: i64, b: i64) -> i64` built from
wrapping arithmetic, bit operations, division by values that are never
zero, shifts, `f32` and `f64` arithmetic (NaN and infinities included),
conversions between every integer and float type, `if`, `match` on ranges,
`for` loops, fixed arrays, a struct, an enum, `Vec(i64)`, `String`,
`Box`, `Map`, `inout` and `sink` parameters, closures that borrow,
iterator pipelines, a generic function and calls to earlier functions. Nothing in them overflows a checked operation, so every
build must print the same, and that must be what Python computes.

    tests/differential.py --compiler out/jihoo             # the fixed seeds CI runs
    tests/differential.py --seed 100 --count 500 --keep d  # more, keeping failures in d
    tests/differential.py --valgrind "$(which valgrind)"   # and check memory
"""

import argparse
import math
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

TYPES = {
    "i8": (8, True), "i16": (16, True), "i32": (32, True), "i64": (64, True),
    "i128": (128, True), "u8": (8, False), "u16": (16, False), "u32": (32, False),
    "u64": (64, False), "u128": (128, False),
}
FLOATS = ("f32", "f64")
ALL = list(TYPES) + list(FLOATS)
FLOAT_LITERALS = ["0.0", "1.0", "0.5", "2.5", "0.1", "3.0", "1000000.0", "0.333",
                  "123456789.0", "4294967296.0"]
INPUTS = 6
STRUCT_FIELDS = [("x", "i64"), ("y", "u16"), ("z", "f32")]
# The most function bodies one call may run.
CALL_BUDGET = 6


def wrap(v, t):
    bits, signed = TYPES[t]
    v &= (1 << bits) - 1
    if signed and v >= 1 << (bits - 1):
        v -= 1 << bits
    return v


FLT_MAX = (2 - 2 ** -23) * 2.0 ** 127


def to_f32(x):
    """The f32 nearest to a float, ties to even."""
    try:
        return struct.unpack("<f", struct.pack("<f", x))[0]
    except OverflowError:
        return math.copysign(math.inf, x)


def int_to_float(v, t):
    """An integer rounded once to the float type."""
    if t == "f64":
        return float(v)
    m, sign = abs(v), -1.0 if v < 0 else 1.0
    b = m.bit_length()
    if b > 24:
        shift = b - 24
        q, r = divmod(m, 1 << shift)
        half = 1 << (shift - 1)
        if r > half or (r == half and q & 1):
            q += 1
        m = q << shift
    x = sign * float(m)
    return math.copysign(math.inf, x) if abs(x) > FLT_MAX else x


def float_to_int(x, t):
    """`as`: towards zero, saturating, NaN is 0."""
    bits, signed = TYPES[t]
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if math.isnan(x):
        return 0
    if math.isinf(x):
        return hi if x > 0 else lo
    return max(lo, min(hi, int(x)))


def fdiv(x, y):
    if y != 0:
        return x / y
    if x == 0 or math.isnan(x):
        return math.nan
    return math.copysign(math.inf, x) * math.copysign(1.0, y)


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
        self.names = 0

    def fresh(self, prefix):
        self.names += 1
        return f"{prefix}{self.names}"

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
        if t in FLOATS:
            return self.fexpr(env_types, t, depth)
        rng = self.rng
        if depth <= 0 or rng.random() < 0.2:
            n = self.var(env_types, t)
            if n is not None and rng.random() < 0.7:
                return (n, t, lambda env, n=n: env[n])
            if rng.random() < 0.5:
                # a conversion from a variable of another type
                n = rng.choice([n for n, nt in env_types.items() if nt in ALL])
                return self.convert((n, env_types[n], lambda env, n=n: env[n]), t)
            return self.literal(t)
        kind = rng.choice(["arith", "arith", "bits", "shift", "div", "conv", "if", "match",
                           "call", "generic"])
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
            s = rng.choice(ALL)
            return self.convert(self.expr(env_types, s, depth - 1), t)
        if kind == "if":
            s = rng.choice(ALL)
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
        if kind == "generic":
            # `mix`, an instance per integer type
            a = self.expr(env_types, t, depth - 1)
            b = self.expr(env_types, t, depth - 1)

            def mix(env, a=a, b=b):
                x, y = a[2](env), b[2](env)
                n = (x & ((1 << bits) - 1)) % bits
                return wrap(((x + y) * x) ^ (y >> n), t)
            return (f"mix({a[0]}, {b[0]})", t, mix)
        # a call of an earlier function
        if self.funcs == 0 or self.loops > 0:
            return self.literal(t)
        k = rng.randrange(self.funcs)
        if self.cost + self.costs[k] > CALL_BUDGET:
            return self.literal(t)
        self.cost += self.costs[k]
        a = self.convert(self.expr(env_types, rng.choice(ALL), depth - 1), "i64")
        b = self.convert(self.expr(env_types, rng.choice(ALL), depth - 1), "i64")

        def call(env, k=k, a=a, b=b):
            return env["#funcs"][k](a[2](env), b[2](env), env["#funcs"])
        return self.convert((f"f{k}({a[0]}, {b[0]})", "i64", call), t)

    def convert(self, e, t):
        s = e[1]
        if s == t:
            return e
        if s in FLOATS and t in FLOATS:
            f = (lambda x: to_f32(x)) if t == "f32" else (lambda x: x)
        elif s in FLOATS:
            f = lambda x: float_to_int(x, t)
        elif t in FLOATS:
            f = lambda x: int_to_float(x, t)
        else:
            f = lambda x: wrap(x, t)
        return (f"({e[0]} as {t})", t, lambda env, e=e, f=f: f(e[2](env)))

    def fexpr(self, env_types, t, depth):
        """A float expression: arithmetic rounded to `t` after each step."""
        rng = self.rng
        r32 = to_f32 if t == "f32" else (lambda x: x)
        if depth <= 0 or rng.random() < 0.25:
            n = self.var(env_types, t)
            if n is not None and rng.random() < 0.6:
                return (n, t, lambda env, n=n: env[n])
            if rng.random() < 0.5:
                n = rng.choice([n for n, nt in env_types.items() if nt in ALL])
                return self.convert((n, env_types[n], lambda env, n=n: env[n]), t)
            lit = rng.choice(FLOAT_LITERALS)
            v = r32(float(lit))
            return (f"({lit} as {t})", t, lambda env, v=v: v)
        kind = rng.choice(["arith", "arith", "arith", "conv", "if"])
        if kind == "arith":
            op = rng.choice(["+", "-", "*", "/"])
            a = self.fexpr(env_types, t, depth - 1)
            b = self.fexpr(env_types, t, depth - 1)
            f = {"+": lambda x, y: x + y, "-": lambda x, y: x - y, "*": lambda x, y: x * y,
                 "/": fdiv}[op]
            return (f"({a[0]} {op} {b[0]})", t,
                    lambda env, a=a, b=b, f=f: r32(f(a[2](env), b[2](env))))
        if kind == "conv":
            return self.convert(self.expr(env_types, rng.choice(ALL), depth - 1), t)
        s = rng.choice(ALL)
        x = self.expr(env_types, s, depth - 1)
        y = self.expr(env_types, s, depth - 1)
        a = self.fexpr(env_types, t, depth - 1)
        b = self.fexpr(env_types, t, depth - 1)

        def choose(env, x=x, y=y, a=a, b=b):
            return a[2](env) if x[2](env) < y[2](env) else b[2](env)
        return (f"(if {x[0]} < {y[0]} {{ {a[0]} }} else {{ {b[0]} }})", t, choose)

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
        kind = rng.choice(["var", "var", "set", "set", "for", "if", "array", "struct", "enum",
                           "vec", "closure", "pipeline", "string", "box", "map",
                           "inout"]) if depth > 0 \
            else rng.choice(["var", "set"])
        assignable = [n for n in env_types if n[0] == "v" or "." in n]
        if kind == "var" or (kind == "set" and not assignable):
            t = rng.choice(ALL)
            e = self.expr(env_types, t, 3)
            n = self.fresh("v")
            env_types[n] = t

            def run(env, n=n, e=e):
                env[n] = e[2](env)
            return [f"{pad}var {n}: {t} = {e[0]}"], run
        if kind == "set":
            n = rng.choice(assignable)
            e = self.expr(env_types, env_types[n], 3)

            def run(env, n=n, e=e):
                env[n] = e[2](env)
            return [f"{pad}{n} = {e[0]}"], run
        if kind == "for":
            k = rng.randint(1, 6)
            i = self.fresh("i")
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
            s = rng.choice(ALL)
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
        # an array: built, one element changed, one read into a variable
        n = self.fresh("v")
        elems = [self.expr(env_types, "i64", 2) for _ in range(4)]
        if kind == "struct":
            return self.struct_stmt(env_types, pad)
        if kind == "enum":
            return self.enum_stmt(env_types, pad)
        if kind == "vec":
            return self.vec_stmt(env_types, pad)
        if kind == "closure":
            return self.closure_stmt(env_types, pad)
        if kind == "pipeline":
            return self.pipeline_stmt(env_types, pad)
        if kind == "string":
            return self.string_stmt(env_types, pad)
        if kind == "box":
            return self.box_stmt(env_types, pad)
        if kind == "map":
            return self.map_stmt(env_types, pad)
        if kind == "inout":
            return self.inout_stmt(env_types, pad)
        at = self.convert(self.expr(env_types, rng.choice(ALL), 2), "i64")
        value = self.expr(env_types, "i64", 2)
        read = self.convert(self.expr(env_types, rng.choice(ALL), 2), "i64")
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

    def struct_stmt(self, env_types, pad):
        """An `S`, whose fields are then variables."""
        n = self.fresh("s")
        fields = [(f, t, self.expr(env_types, t, 2)) for f, t in STRUCT_FIELDS]
        for f, t, _ in fields:
            env_types[f"{n}.{f}"] = t

        def run(env, n=n, fields=fields):
            values = [(f, e[2](env)) for f, _, e in fields]
            for f, v in values:
                env[f"{n}.{f}"] = v
        inits = ", ".join(f"{f}: {e[0]}" for f, _, e in fields)
        return [f"{pad}var {n} = S {{ {inits} }}"], run

    def enum_stmt(self, env_types, pad):
        """An `E` made by one of its variants, then matched into a variable."""
        rng = self.rng
        sel_t = rng.choice(list(TYPES))
        sel = self.expr(env_types, sel_t, 2)
        a = self.expr(env_types, "i64", 2)
        b1 = self.expr(env_types, "i32", 2)
        b2 = self.expr(env_types, "f64", 2)
        c = self.expr(env_types, "i64", 2)
        k = rng.randint(1, 99)
        e = self.fresh("e")
        n = self.fresh("v")

        def run(env, n=n, sel=sel, a=a, b1=b1, b2=b2, c=c, k=k):
            which = sel[2](env) & 3
            if which == 0:
                env[n] = wrap(a[2](env) * k, "i64")
            elif which == 1:
                env[n] = wrap(b1[2](env), "i64") ^ float_to_int(b2[2](env), "i64")
            else:
                env[n] = c[2](env)
        lines = [
            f"{pad}var {e}: E = match {sel[0]} & (3 as {sel_t}) {{ 0 => E.A({a[0]}), "
            f"1 => E.B({b1[0]}, {b2[0]}), _ => E.C }}",
            f"{pad}var {n}: i64 = match {e} {{ A(p) => p *% {k}, "
            f"B(p, q) => (p as i64) ^ (q as i64), C => {c[0]} }}",
        ]
        env_types[e] = "E"
        env_types[n] = "i64"
        return lines, run

    def vec_stmt(self, env_types, pad):
        """A `Vec(i64)` filled by a loop, indexed and popped."""
        rng = self.rng
        k = rng.randint(1, 8)
        w = self.fresh("w")
        i = self.fresh("i")
        inner = dict(env_types)
        inner[i] = "i64"
        self.loops += 1
        e = self.expr(inner, "i64", 2)
        self.loops -= 1
        idx = self.convert(self.expr(env_types, rng.choice(ALL), 2), "i64")
        n = self.fresh("v")
        m = self.fresh("v")

        def run(env, w=w, i=i, e=e, idx=idx, n=n, m=m, k=k):
            values = []
            for x in range(k):
                env[i] = x
                values.append(e[2](env))
            del env[i]
            env[n] = values[(idx[2](env) & 1023) % len(values)]
            env[m] = wrap(values.pop() + len(values), "i64")
        lines = [
            f"{pad}var {w} = Vec(i64).new()",
            f"{pad}for {i} in 0..{k} {{ {w}.push({e[0]}) }}",
            f"{pad}var {n}: i64 = {w}[({idx[0]} & 1023) % {w}.len()]",
            f"{pad}var {m}: i64 = (match {w}.pop() {{ Some(p) => p, None => 0 }}) +% {w}.len()",
        ]
        env_types[w] = "Vec"
        env_types[n] = "i64"
        env_types[m] = "i64"
        return lines, run

    def lambda_body(self, env_types, params):
        """An expression over `params` (i64s) and what a closure captures."""
        inner = dict(env_types)
        for x in params:
            inner[x] = "i64"
        self.loops += 1
        e = self.expr(inner, "i64", 2)
        self.loops -= 1
        return e

    def closure_stmt(self, env_types, pad):
        """A closure that borrows variables, called twice."""
        g, x = self.fresh("g"), self.fresh("x")
        body = self.lambda_body(env_types, [x])
        a = self.expr(env_types, "i64", 2)
        b = self.expr(env_types, "i64", 2)
        n = self.fresh("v")

        def run(env, g=g, x=x, body=body, a=a, b=b, n=n):
            def call(v):
                inner = dict(env)
                inner[x] = v
                return body[2](inner)
            env[n] = wrap(call(a[2](env)) + call(b[2](env)), "i64")
        env_types[n] = "i64"
        # The arguments are computed first: while the closure lives, what
        # it borrows mutably (the `var`s it uses) cannot be read elsewhere.
        ta, tb = self.fresh("t"), self.fresh("t")
        return [f"{pad}let {ta} = {a[0]}", f"{pad}let {tb} = {b[0]}",
                f"{pad}let {g} = fn({x}: i64) -> i64 {{ {body[0]} }}",
                f"{pad}var {n}: i64 = {g}({ta}) +% {g}({tb})"], run

    def pipeline_stmt(self, env_types, pad):
        """`iter.range(..).map(..).filter(..).take(..).fold(..)`."""
        rng = self.rng
        x, y, acc = self.fresh("x"), self.fresh("x"), self.fresh("x")
        k = rng.randint(0, 9)
        take = rng.randint(0, 6)
        parity = rng.randint(0, 1)
        body = self.lambda_body(env_types, [x])
        start = self.expr(env_types, "i64", 2)
        n = self.fresh("v")

        def run(env, x=x, body=body, start=start, n=n):
            def m(v):
                inner = dict(env)
                inner[x] = v
                return body[2](inner)
            items = [m(v) for v in range(k)]
            kept = [v for v in items if v & 1 == parity][:take]
            total = start[2](env)
            for v in kept:
                total = wrap(total + v, "i64")
            env[n] = total
        env_types[n] = "i64"
        # the start first, as for closure_stmt's arguments
        t = self.fresh("t")
        return [f"{pad}let {t} = {start[0]}",
                f"{pad}var {n}: i64 = iter.range(0, {k}).map(fn({x}) {{ {body[0]} }})"
                f".filter(fn({y}) {{ ({y} & (1 as i64)) == ({parity} as i64) }}).take({take})"
                f".fold({t}, fn({acc}, {y}) {{ {acc} +% {y} }})"], run

    def string_stmt(self, env_types, pad):
        """A `String` of integers' digits: cloned, given to `take` (`sink`),
        indexed by byte."""
        rng = self.rng
        k = rng.randint(1, 4)
        st, i, c, n = self.fresh("s"), self.fresh("i"), self.fresh("c"), self.fresh("v")
        t = rng.choice(list(TYPES))
        inner = dict(env_types)
        inner[i] = "i64"
        self.loops += 1
        e = self.expr(inner, t, 2)
        self.loops -= 1
        t2 = rng.choice(list(TYPES))
        last = self.expr(env_types, t2, 2)
        idx = self.convert(self.expr(env_types, rng.choice(ALL), 2), "i64")

        def run(env, i=i, e=e, last=last, idx=idx, n=n, k=k):
            text = ""
            for x in range(k):
                env[i] = x
                text += str(e[2](env))
            del env[i]
            text += str(last[2](env))
            data = text.encode()
            env[n] = wrap(len(data) + data[(idx[2](env) & 1023) % len(data)] + len(data), "i64")
        env_types[n] = "i64"
        return [
            f"{pad}var {st} = String.new()",
            f"{pad}for {i} in 0..{k} {{ ({e[0]}).show(&{st}) }}",
            f"{pad}{st}.push_str(({last[0]}).to_string().as_str())",
            f"{pad}let {c} = {st}.clone()",
            f"{pad}var {n}: i64 = {st}.len() +% ({st}.as_bytes()[({idx[0]} & 1023) % {st}.len()]"
            f" as i64) +% take({c})",
        ], run

    def box_stmt(self, env_types, pad):
        """A boxed integer changed through `*`, and a boxed `Vec` pushed to
        through `deref_mut`."""
        b, bw, n = self.fresh("b"), self.fresh("b"), self.fresh("v")
        e = self.expr(env_types, "i64", 2)
        d = self.expr(env_types, "i64", 2)
        x = self.expr(env_types, "i64", 2)

        def run(env, e=e, d=d, x=x, n=n):
            v = wrap(e[2](env) + d[2](env), "i64")
            env[n] = wrap(v + x[2](env) + 2, "i64")
        env_types[n] = "i64"
        return [
            f"{pad}var {b} = Box.new({e[0]})",
            f"{pad}*{b} = *{b} +% {d[0]}",
            f"{pad}var {bw} = Box.new(Vec(i64).new())",
            f"{pad}{bw}.push({x[0]})",
            f"{pad}{bw}.push(*{b})",
            f"{pad}var {n}: i64 = (*{bw})[1] +% (*{bw})[0] +% {bw}.len()",
        ], run

    def map_stmt(self, env_types, pad):
        """A `Map(i64, i64)`: inserts that may replace, then a lookup."""
        rng = self.rng
        k = rng.randint(1, 6)
        m, i, n = self.fresh("m"), self.fresh("i"), self.fresh("v")
        inner = dict(env_types)
        inner[i] = "i64"
        self.loops += 1
        key = self.convert(self.expr(inner, rng.choice(list(TYPES)), 2), "i64")
        value = self.expr(inner, "i64", 2)
        self.loops -= 1
        q = self.convert(self.expr(env_types, rng.choice(list(TYPES)), 2), "i64")

        def run(env, i=i, key=key, value=value, q=q, n=n, k=k):
            table = {}
            for x in range(k):
                env[i] = x
                kk = key[2](env) & 7
                table[kk] = value[2](env)
            del env[i]
            env[n] = wrap(len(table) + table.get(q[2](env) & 7, 0), "i64")
        env_types[n] = "i64"
        return [
            f"{pad}var {m} = Map(i64, i64).new()",
            f"{pad}for {i} in 0..{k} {{ {m}.insert({key[0]} & 7, {value[0]}) }}",
            f"{pad}var {n}: i64 = {m}.len() +% (match {m}.get({q[0]} & 7) {{ Some(x) => x, "
            f"None => 0 }})",
        ], run

    def inout_stmt(self, env_types, pad):
        """`bump(&v, d)`: an `inout` parameter."""
        n = self.fresh("v")
        e = self.expr(env_types, "i64", 2)
        d = self.expr(env_types, "i64", 2)

        def run(env, n=n, e=e, d=d):
            v = e[2](env)
            env[n] = wrap(v + d[2](env), "i64")
        env_types[n] = "i64"
        return [f"{pad}var {n}: i64 = {e[0]}", f"{pad}bump(&{n}, {d[0]})"], run

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
    out = ["import std.env", "import std.iter", "",
           "fn mix(comptime T: Int, x: T, y: T) -> T { ((x +% y) *% x) ^ (y >> x) }",
           "fn bump(inout x: i64, d: i64) { x = x +% d }",
           "fn take(sink s: String) -> i64 { s.len() }",
           "struct S { " + ", ".join(f"{f}: {t}" for f, t in STRUCT_FIELDS) + " }",
           "enum E { A(i64), B(i32, f64), C }", ""] + texts + [""]
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


def check(compiler, seed, functions, directory, keep, valgrind=None):
    source, expected = program(seed, functions)
    path = Path(directory) / f"diff{seed}.jh"
    path.write_text(source)
    problems = []
    if valgrind:
        # memory errors and leaks in the drops of String, Vec, Box and Map
        exe = Path(directory) / f"diff{seed}"
        b = subprocess.run([str(compiler), "build", "-O0", str(path), "-o", str(exe)],
                           capture_output=True, text=True, timeout=600)
        if b.returncode != 0:
            problems.append(f"seed {seed} -O0 build: exit {b.returncode}\n{b.stderr[-2000:]}")
        else:
            r = subprocess.run([str(valgrind), "-q", "--leak-check=full",
                                "--errors-for-leak-kinds=definite", "--error-exitcode=9",
                                str(exe)], capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                problems.append(f"seed {seed} valgrind: exit {r.returncode}\n{r.stderr[-3000:]}")
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
    parser.add_argument("--valgrind", type=Path,
                        help="also run each program, built at -O0, under this valgrind")
    args = parser.parse_args()
    if args.print:
        print(program(args.seed, args.functions)[0])
        return
    compiler = args.compiler.resolve()
    failed = 0
    with tempfile.TemporaryDirectory(prefix="jihoo-differential-") as directory:
        for seed in range(args.seed, args.seed + args.count):
            problems = check(compiler, seed, args.functions, directory, args.keep, args.valgrind)
            for p in problems:
                print(p)
            failed += bool(problems)
    print(f"{args.count - failed} of {args.count} programs agree")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
