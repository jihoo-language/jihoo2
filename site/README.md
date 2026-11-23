# jihoo2 website

The documentation site, built with [Astro](https://astro.build) (no theme
framework: the layouts, styles, search and landing page are in `src/`), and
published to GitHub Pages by
[`.github/workflows/pages.yml`](../.github/workflows/pages.yml).

Most pages are generated, so that `docs/` and `examples/` stay the only source:
`npm run sync` (part of `dev` and `build`) runs
[`scripts/sync-docs.mjs`](scripts/sync-docs.mjs), which turns

- every Markdown file in `docs/` into a page. The table of contents in
  [`docs/README.md`](../docs/README.md) decides where each one goes and the order
  of the sidebar ([`scripts/toc.mjs`](scripts/toc.mjs) reads it). Relative links
  between the files become links between pages;
- `examples/*.jh` into example pages, with their header comment as the text;
- the sidebar into `src/generated/nav.json`, which the docs layout reads.

To add a page, write `docs/<group>/<name>.md` starting with a `# Title` line and
list it in `docs/README.md`. The sync fails on a page missing from the table of
contents or a link to a file that does not exist, and `npm run build` then runs
[`scripts/check-links.mjs`](scripts/check-links.mjs), which fails on a link or
`#anchor` that leads nowhere in the built site.

| path | what |
|------|------|
| `src/pages/index.astro` | the landing page |
| `src/pages/[...slug].astro` | every docs and example page |
| `src/pages/search.json.ts` | the search index the search dialog (Ctrl K) loads |
| `src/layouts/Base.astro` | header, footer, theme toggle, search, code-block frames |
| `src/layouts/Docs.astro` | sidebar, "On this page", previous/next |
| `src/styles/global.css` | every style, for both themes |
| `src/content/docs/start/getting-started.md` | the only page written here |

Syntax highlighting for jihoo code comes from
[`src/jihoo.tmLanguage.json`](src/jihoo.tmLanguage.json); a code block's
`title="..."` becomes the bar above it.

```sh
nix develop .#site     # Node.js
cd site
npm ci
npm run dev            # http://localhost:4321/jihoo2/
npm run build          # into dist/
```

`SITE_URL` and `BASE_PATH` override the default address
(`https://jihoo-language.github.io` + `/jihoo2`); the deploy workflow sets them
from the repository's Pages settings. Every push to `main` that touches `site/`,
`docs/` or `examples/` rebuilds the site.
