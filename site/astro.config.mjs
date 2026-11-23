// @ts-check
import fs from 'node:fs';
import { defineConfig } from 'astro/config';

// Syntax highlighting for ```jihoo code blocks.
const jihoo = JSON.parse(fs.readFileSync(new URL('./src/jihoo.tmLanguage.json', import.meta.url), 'utf-8'));

// ```lang title="file.jh" puts the title above the block: the layout draws the
// frame from data-title (see src/styles/global.css and the copy button script).
/** @type {import('shiki').ShikiTransformer} */
const titles = {
  name: 'jihoo:titles',
  pre(node) {
    const title = this.options.meta?.__raw?.match(/title="([^"]*)"/)?.[1];
    if (title) node.properties['data-title'] = title;
  },
};

// GitHub Pages serves the site under /<repository>/. The deploy workflow passes
// the exact origin and path; these defaults match github.com/jihoo-language/jihoo2.
export default defineConfig({
  site: process.env.SITE_URL ?? 'https://jihoo-language.github.io',
  base: process.env.BASE_PATH ?? '/jihoo2',
  trailingSlash: 'always',
  markdown: {
    shikiConfig: {
      themes: { light: 'github-light', dark: 'github-dark-dimmed' },
      defaultColor: false,
      langs: [jihoo],
      transformers: [titles],
    },
  },
});
