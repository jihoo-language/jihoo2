// The search index: every page's title, section, headings and plain text. The
// search dialog in src/layouts/Base.astro loads it the first time it opens.
import type { APIRoute } from 'astro';
import { getCollection, render } from 'astro:content';
import { pages, url } from '../lib/site';

/** Markdown to searchable text: code kept, syntax dropped. */
function plain(md: string): string {
  return md
    .replace(/^```.*$/gm, ' ')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/<[^>]+>/g, ' ')
    .replace(/[#>*_`|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export const GET: APIRoute = async () => {
  const groups = new Map(pages().map((p) => [p.slug, p.group]));
  const entries = await getCollection('docs');
  const index = await Promise.all(
    entries.map(async (entry) => {
      const { headings } = await render(entry);
      return {
        t: entry.data.title,
        u: url(entry.id),
        g: groups.get(entry.id) ?? 'Docs',
        d: entry.data.description ?? '',
        h: headings.map((h) => h.text),
        b: plain(entry.body ?? '').slice(0, 6000),
      };
    }),
  );
  return new Response(JSON.stringify(index), { headers: { 'Content-Type': 'application/json' } });
};
