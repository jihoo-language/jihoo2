# Bootstrap seed

These files are byte-for-byte copies of the selected jihoo release archives.
Their version and SHA-256 hashes are pinned in `flake.nix`; their fixed local
names keep only the current pair in the checkout. Nix verifies the raw archive
hash and patches the released compiler for its environment as before.

The current seed is jihoo2 v0.1.0, the first jihoo2 release, which itself was
built from the jihoo v0.5.0 seed:

| Local file | Original release asset | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `jihoo-x86_64-linux.tar.gz` | [x86_64 archive](https://github.com/jihoo-language/jihoo2/releases/download/v0.1.0/jihoo-v0.1.0-x86_64-linux.tar.gz) | 991,012 | `f00489601bb505d2454365b817bc5a471ca31483168621633efdea13060d104e` |
| `jihoo-aarch64-linux.tar.gz` | [aarch64 archive](https://github.com/jihoo-language/jihoo2/releases/download/v0.1.0/jihoo-v0.1.0-aarch64-linux.tar.gz) | 942,304 | `579a07e92e5cbea588cbb4dbc34bafbf2c636d8aee4423db286765ec9fc1905e` |

Each archive contains the compiler, its standard library and Apache-2.0 license.
No binaries were rebuilt or recompressed for vendoring: both were checked
against the release's published `.sha256` files by `scripts/bump-version.sh seed`.

Run `python3 tests/bootstrap_seed.py` to check the current pair. To select a
new release, use `scripts/bump-version.sh seed X.Y.Z`, then review and test its
commit before pushing. `flake.nix` is authoritative if this table falls behind.

See [self-hosting](../docs/internals/self-hosting.md) for the release process,
offline limits and bootstrap trust assumptions. Do not use Git LFS here or
replace an archive with an unverified rebuild.
