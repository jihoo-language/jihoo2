# jihoo

A systems programming language that is as fast as C and memory-safe without
lifetime annotations or a garbage collector.

This repository, jihoo2, is [jihoo](https://github.com/jihoo-language/jihoo)
without dependent types: the dependent core (`#![dependent_core]`, `jihoo core`,
`.jhc` files), its `lib/dependent` libraries and its Lean proofs are not part
of it. Contracts are still proved by the verifier over MIR.

- **Ownership without lifetimes.** Every value has one owner. Functions
  borrow their arguments (`x: T`), borrow them to change them (`inout x: T`)
  or take them (`sink x: T`). Borrows are scoped to a function body, so the
  compiler sees all of them and needs no annotations.
- **No hidden costs.** No GC, no reference counting, no implicit copies of
  anything that owns memory: those move, and `.clone()` copies them.
- **As fast as C.** Programs compile through LLVM; on the benchmarks they
  match C compiled by clang, with bounds checks on.
- **Generics at compile time**, as in Zig: `comptime T: type` parameters,
  inferred at calls, with traits as bounds.
- **A standard library written in jihoo** (`Vec`, `String`, `Map`, `Box`,
  sorting, files, processes), with or without libc.

Documentation: **<https://jihoo-language.github.io/jihoo2/>**, built from
[docs/](docs) and [examples/](examples).

```jihoo
enum Expr {
    Num(i64)
    Add(Box(Expr), Box(Expr))
}

fn eval(e: Expr) -> i64 {
    match e {
        Num(n) => n
        Add(a, b) => eval(a) + eval(b)
    }
}

fn main() {
    var counts = Map(String, i64).new()
    for w in "a b a".split(" ") {
        match counts.get_mut(w) {
            Some(n) => n += 1
            None => {
                counts.insert(w.to_string(), 1)
            }
        }
    }
    println(counts["a"])                                        // 2
    println(eval(Expr.Add(Box.new(Expr.Num(2)), Box.new(Expr.Num(3)))))  // 5
}
```

```jihoo
fn main() {
    var v = Vec(i64).new()
    v.push(1)
    for x in v {
        v.push(x)   // error: `v` is changed here while it is borrowed
    }
}
```

## Install

```sh
nix run github:jihoo-language/jihoo2 -- run hello.jh     # try it
nix profile install github:jihoo-language/jihoo2         # install `jihoo`
```

Without Nix, each [release](https://github.com/jihoo-language/jihoo2/releases)
has the compiler for x86_64 and aarch64 Linux with its standard library; it
needs glibc and LLVM 21 (libLLVM, clang, lld).

## Build from source

jihoo is written in jihoo. Everything comes from `flake.nix` (libLLVM,
clang and lld from LLVM 21, and the seed: the compiler of the latest
selected release, bundled in `bootstrap/` with pinned hashes). Building does
not download the seed from GitHub Releases; Nix may still download its other
dependencies. `nix build` bootstraps the compiler: the seed builds
`compiler/`, which builds itself again and is installed.

```sh
nix build                                    # ./result/bin/jihoo
nix develop                                  # `jihoo` is the seed
jihoo build -O2 compiler/main.jh -o out/jihoo
out/jihoo run examples/hello.jh
out/jihoo check examples/hello.jh              # no code generation or linking
out/jihoo fmt examples/hello.jh                # formatted source on stdout
out/jihoo emit-mir examples/hello.jh
jihoo run tests/runner.jh                    # the tests
```

## Layout

| path | what |
|------|------|
| `compiler` | the compiler, in jihoo: parser, type checker, MIR, borrow checker, LLVM IR writer |
| `lib/std` | the standard library, in jihoo |
| `bootstrap` | checksum-pinned release archives used as the self-hosting seed |
| `tests/run`, `tests/fail` | programs and their expected output or error; `tests/runner.jh` runs them |
| `examples` | example programs, which the website shows |
| `benchmarks` | backend, iterator fusion and [versus C](benchmarks/versus-c/README.md) benchmarks, with recorded measurements |
| `docs` | the documentation, one Markdown file per page |
| `site` | the website ([site/README.md](site/README.md)) |

## Status

jihoo2 version 0.1, released as
[v0.1.0](https://github.com/jihoo-language/jihoo2/releases/tag/v0.1.0):
jihoo's development line after 0.5.0 without the dependent core, with
versions starting again at 0.1.0 ([roadmap](docs/internals/roadmap.md#direction)). It keeps jihoo's 0.1
rewrite, which broke compatibility with everything
before it ([why](docs/internals/redesign.md)), with a self-hosted compiler
and checking, testing, diagnostic and formatting tools. Native (libc) and
freestanding (no libc, static) programs for x86_64 and aarch64 Linux, and
[UEFI applications](docs/language/profiles.md#uefi) (PE32+ images) for
both machines.

Experimental observer-indexed history types ask how two resource schedules
can be told apart. `history.Observe(H, S)` computes the final observation
at a slot. `history.Separation(H, K, S)` computes the fewest ordered samples
that can distinguish the schedules, yielding results such as `Apart(3)`.
The checker validates reads and transfers throughout both histories.
[Observer-indexed history types](docs/language/observed-histories.md)

Experimental graph divisor types describe placements related by local
exchanges. On a triangle a single credit at one vertex and a single credit
at its neighbor have different types, while three at either vertex have
the same class. The compiler computes that minimum of three as a type.
[Graph divisor types](docs/language/divisor-classes.md)

Ordinary modules can compute a type by matching another type against the
equations of a closed type family. For example, `Dual(Client)` derives the
server side of a protocol, including the payload and order of its replies.
The result uses the layout and move rules of the type it reduces to.
[Closed type families](docs/language/type-families.md)

The compiler uses its own threads: it elaborates, infers borrow effects and
checks borrows one function per thread, the costliest first, and emits
large programs in parallel code generation units, with the same output as
one thread. On 8 CPUs, `jihoo check` of the compiler takes 0.53 s and a
full `-O2` build 15.2 s, against 17.0 s and 36.8 s at jihoo 0.2.1
([phase timings](docs/reference/command-line.md#phase-timings),
[roadmap](docs/internals/roadmap.md#4-parallel-compilation-03)).

Done: ownership with moves, destructors and drop flags; borrowing with
parameter conventions, projections and views, checked by a separate borrow
kernel; integers up to 128 bits; structs, enums, tuples, arrays, slices;
`match` with exhaustiveness, `|` patterns that bind names, `let ... else`,
and `match`es that are views of the borrowed parts they give; generics
with inference; traits with default
methods and generic impls; closures that borrow, `comptime` closures
that inline, and `move fn` closures that own their captures without
allocating, returned as `-> impl Iterator` or `-> impl fn(A) -> R`; `Option`, `Result` and `?`; `unsafe` pointers, `static`
globals, C functions and callbacks (including structs by value, 128-bit
integers and variadic function pointers on both targets), dynamic libraries
(`std.ffi`), system calls, atomics and volatile accesses for device
registers; [panic handlers](docs/language/errors.md#panic-handlers) for
library objects that another operating system links, and
[kernel code generation](docs/language/profiles.md#kernel-code-generation)
without the red zone or vector registers; the standard library,
including running other programs.

[Compile-time evaluation](docs/language/comptime.md) runs ordinary functions
and blocks in a separate MIR interpreter for `const` values, `comptime`
expressions, array lengths and compile-time arguments. It supports loops,
recursion, aggregates and the heap: `Vec`, `String`, `Map` and destructors
work, and a `String` or `Vec` result becomes a static `str` or slice.
External calls remain unavailable during evaluation.
[`--comptime-report PATH`](docs/reference/command-line.md#compile-time-resource-reports)
records evaluator steps, cumulative allocations and per-function costs;
failures report resource use and the originating compile-time obligation.

[Verification](docs/reference/command-line.md#verification)
is in the compiler: `jihoo verify` proves bounds and overflow checks,
panics that cannot be reached (also at each call to a small function that
may panic, such as `v[i]`), and
[contracts](docs/language/contracts.md) (`requires`, `ensures`, loop
`invariant`s and structs' type invariants, over integers, fields and
lengths, with `&&`, `||` and `!`) with linear certificates
that a small trusted checker accepts. Every build verifies: it removes
the proved checks, and an unproved contract is a compile error
(`--no-verify` keeps every check). It runs on the compiler's own source
(`jihoo verify compiler/main.jh`), and CI fails if it proves fewer; no
check is left in the trusted checker itself. Certificates may round over
the integers (Chvátal–Gomory cuts), `/` and `%` by constants are followed,
clauses chain order comparisons (`0 <= lo <= i < xs.len`), and a kept check
says what is known of its value (`known 0..=2^33 - 2`). The verifier is
not proved sound; `--audit-proved` keeps the checks it proves, failing as
verifier bugs, to test it on real inputs. `#[no_alloc]` and `#[no_panic]`
are checked through every call, after the proved checks are gone.
`std.modint` has the integers modulo `N`, and `Box.map` rewrites a value
in its own allocation. [Benchmarks against C](benchmarks/versus-c/README.md)
compare with clang and GCC on the programs Ruach and Shalem use.

Buffered UTF-8 input (`std.io.BufRead`, `StrReader`, `BufReader`) and
Unicode scalar decoding/iteration (`std.utf8`) support streaming parsers.
[`std.os`](docs/reference/standard-library.md#files-and-errors) reports
failures as an errno-based `os.Error`, which `std.fs` and `std.process`
also return, and reads and writes bytes with one system call each, so
partial operations, the end of input, interrupted calls and nonblocking
readiness are explicit, in both profiles.
[`std.fmt`](docs/reference/standard-library.md#formatting) formats several
values with patterns such as `"{:>8.2} {:#x}"`, checked while compiling
when they are literals ([`#[literal_check]`](docs/language/comptime.md#checking-literal-arguments)),
and floats print the shortest text that reads back as the same value.
The borrow kernel infers argument escape effects for direct calls, including
recursive functions, so methods on views keep only the borrows they can
actually retain; unknown calls and raw memory effects remain conservative.

[Reflection](docs/language/reflection.md): `inline for` and `inline if`
unroll and select code at compile time, and builtins name a type's fields
and variants. `#[derive(Clone, Eq, Ord, Hash, Show, Default)]` is library code
written with them, in `std/derive.jh`, and keeps privacy.

[Threads](docs/language/threads.md): `std.thread.scope`, `join`, `each`, `map` and
`each_mut` run work on several threads that borrows what the caller owns,
in native and freestanding programs; the `_uneven` forms hand out one item
at a time, for work of very different sizes.
Scopes start any number of workers, collect results with `handle.join()`
and join every remaining worker before the body's locals go away.
The borrow rules and a compile-time check of the closures rule out data
races. `std.sync.Atomic`, `Mutex`, `Channel` and `Once` are shared through
plain borrows.

[Data by index](docs/language/data-oriented.md): `std.arena` has typed ids
and arenas, generational handles and pools, and `Soa(T)`, a
struct-of-arrays vector that reflection lays out from `T`'s fields.

[Dynamic dispatch](docs/language/dyn.md): `dyn Trait` values own a value of
any type with the trait and call it through a table of functions;
`dyn fn` closures own what they capture, so they can be returned and
stored.

[Iterator pipelines](docs/language/iterators.md) compose `map`, `filter`,
`take`, `zip` and `fold` without intermediate collections. Callback types
are inferred, and compile-time closures can be named, captured and forwarded.
Supported pipelines also run in the compile-time interpreter.

LLVM 21's C API optimizes and emits objects directly, with loop and SLP
vectorization at `-O2`/`-O3`. Native linking uses a C driver; freestanding
linking uses lld, and UEFI linking lld's COFF linker. `JIHOO_BACKEND=clang` selects the previous backend for
comparison. [Backend benchmarks](docs/internals/testing.md#backend-performance)
compare compilation time and generated program speed.
[Compilation baselines](docs/internals/testing.md#compilation-time-and-memory)
measure checking and full compilation of the compiler and compile-time
generation workloads, with elapsed time and peak memory, and compare them
with a recorded report.

The compiler is written in jihoo, in `compiler/`, and
[self-hosted](docs/internals/self-hosting.md): the compiler of the latest
release builds it, and it builds itself to the same LLVM IR. There is no
Rust left; the Rust compiler that first bootstrapped it is in the history
up to v0.1.1.

Errors come several per run, in source order, with notes on where a
moved value went and where a conflicting borrow started.

[`jihoo check`](docs/reference/command-line.md#checking-without-code-generation)
checks types, moves, borrows and compile-time obligations without generating
code or requiring LLVM or a linker. `--lib` checks modules without `main`.
`--error-format=json` reports source errors with byte ranges and related
notes for editor integration, and every other failure (usage, files, the
backend and linker) in the same document; text remains the default.
[`jihoo fmt`](docs/reference/command-line.md#source-formatting) formats
source files while preserving comments, literals and meaningful line breaks.
Long argument, parameter and field lists, one-line blocks and `match`es
wrap at a soft width of 100 byte columns, and longer expressions break
after their loosest operators; `--width N` adjusts it. `--check` checks layout without printing
formatted source and `--write` rewrites files, including every `.jh` file in
the directories given. The output is reparsed and must retain the same syntax tree.

[`-g`](docs/reference/command-line.md#debug-information) writes DWARF line
tables, so gdb, lldb, perf and valgrind name the source line of each
frame and instruction, and every variable with its type and scope; gdb
shows `str`, `String`, `Vec`, slices and `Map` by their contents.

[`jihoo lsp`](docs/reference/command-line.md#language-server) is a language
server built on the same checking and diagnostics: errors in unsaved
documents, go to definition and hover (members by the type the compiler
resolved), references, rename (refused when the renamed program would not
check), completion of names and members, signature help, highlights,
inferred types as inlay hints, a quick fix for `let` bindings that are
changed, document outlines and formatting.

Next ([roadmap](docs/internals/roadmap.md)): application-driven library work,
and jihoo2 0.2.0, after which the compiler can use what came after 0.1.0.
