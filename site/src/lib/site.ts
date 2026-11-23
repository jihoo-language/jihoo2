// What every layout needs to know about the site: its links, and the sidebar
// that scripts/sync-docs.mjs writes from docs/README.md and examples/.
import nav from '../generated/nav.json';

export const GITHUB = 'https://github.com/jihoo-language/jihoo2';
export const VERSION = '0.1.0';
export const INSTALL = 'nix run github:jihoo-language/jihoo2 -- run hello.jh';

export type NavPage = { label: string; slug: string; badge?: string };
export type NavGroup = { label: string; items: (NavPage | NavGroup)[] };

export const sidebar = nav as NavGroup[];

/** A URL inside the site, under the base path GitHub Pages serves it from. */
export function url(path = ''): string {
  const base = import.meta.env.BASE_URL.replace(/\/?$/, '/');
  const clean = path.replace(/^\/+/, '');
  return clean === '' ? base : `${base}${clean.replace(/\/?$/, '/')}`;
}

export function isPage(item: NavPage | NavGroup): item is NavPage {
  return 'slug' in item;
}

/** Every page of the sidebar, in reading order. */
export function pages(groups: (NavPage | NavGroup)[] = sidebar): (NavPage & { group: string })[] {
  const out: (NavPage & { group: string })[] = [];
  const walk = (items: (NavPage | NavGroup)[], group: string) => {
    for (const item of items) {
      if (isPage(item)) out.push({ ...item, group });
      else walk(item.items, group);
    }
  };
  for (const g of groups) walk(g.items, g.label);
  return out;
}

/** Whether a group holds the page `slug`, so that it starts open. */
export function contains(group: NavGroup, slug: string): boolean {
  return group.items.some((item) => (isPage(item) ? item.slug === slug : contains(item, slug)));
}
