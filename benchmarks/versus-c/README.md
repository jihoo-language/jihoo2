# jihoo against C

Seven small programs, each written in jihoo and in C, that the sibling
languages use to compare themselves with C: `fib`, `sieve`, `nbody`, `trees`
and `sort` from [Ruach](https://github.com/jihoo-language/ruach), `collatz`
and `matmul` from [Shalem](https://github.com/jihoo-language/shalem). The C
files are theirs, unchanged except that `matmul.c` prints its trace with
`%.1f`. The same numbers measure the three languages against one baseline.

```sh
python3 benchmarks/versus-c/run.py            # all of them, best of three runs
python3 benchmarks/versus-c/run.py fib trees  # some
```

`run.py` builds each program with `jihoo build -O2` (overflow checks on,
except those the verifier proves away), with `-O2 --release` (`+ - *`
wrap) and each C program with `clang -O2` and `gcc -O2`, checks that all
of them print the same thing, and prints the best wall-clock time of each. Bounds checks stay on in both
jihoo builds unless they are proved. The C programs are built with clang,
which shares jihoo's backend (LLVM 21), and with GCC. `JIHOO`, `CLANG`,
`GCC` and `RUNS` choose the compilers and the number of runs. In the dev
shell `CC` is GCC, so a comparison with "C" must say which compiler.

| benchmark | what it exercises |
| --- | --- |
| `fib` | naive Fibonacci of 42: calls |
| `sieve` | primes below 50 million, five times: a `Vec(bool)` and its bounds checks |
| `nbody` | five bodies, 50 million steps: floats in an array of structs |
| `trees` | binary trees of depth 20, allocated and freed: `Box` and drops |
| `sort` | 5 million integers sorted with a closure, against C's `qsort` |
| `collatz` | the longest Collatz chain below 5 million: `u64` arithmetic in a loop |
| `matmul` | a 1000×1000 `f64` product: indexing `i * n + j` |

## Results

x86_64, best of three runs, 2026-11-24 (jihoo 0.5 from this checkout,
clang 21.1.8, GCC 16.2.0), under WSL2; the ratios are of the `--release`
build:

| benchmark | jihoo `-O2` | `--release` | clang `-O2` | GCC `-O2` | / clang | / GCC |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `fib` | 0.93 s | 0.54 s | 0.50 s | 0.28 s | 1.08 | 1.92 |
| `sieve` | 1.17 s | 1.04 s | 1.01 s | 1.03 s | 1.03 | 1.01 |
| `nbody` | 1.65 s | 1.66 s | 1.69 s | 1.76 s | 0.98 | 0.94 |
| `trees` | 6.45 s | 6.21 s | 5.60 s | 5.14 s | 1.11 | 1.21 |
| `sort` | 0.28 s | 0.26 s | 0.53 s | 0.57 s | 0.50 | 0.46 |
| `collatz` | 1.04 s | 0.55 s | 0.52 s | 0.98 s | 1.07 | 0.56 |
| `matmul` | 0.20 s | 0.20 s | 0.19 s | 0.17 s | 1.04 | 1.13 |

What the numbers say:

- Against clang, the same backend, `--release` is within about 10%.
  `fib` compiles to the same machine code as clang's; GCC unrolls its
  recursion, which LLVM does not. GCC's `collatz` is the slower one here;
  that was not looked into.
- `sort` is twice as fast because `sort.sort_by` takes its comparison as a
  `comptime` closure and inlines it; `qsort` calls a function pointer per
  comparison.
- The checked build pays for the overflow checks the verifier cannot
  remove: `fib(n - 1) + fib(n - 2)` adds two results of calls, and
  `collatz`'s `3 * m + 1` has no bound. Elsewhere it costs little or
  nothing.
- `trees` allocates a 24-byte node (a tag and two boxes) where C's is 16
  bytes, and frees each node through the drop glue of `Box(Tree)`.
