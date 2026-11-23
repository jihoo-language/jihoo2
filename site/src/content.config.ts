import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { z } from 'astro/zod';

// Every page under src/content/docs: Getting started, written here, and the
// pages scripts/sync-docs.mjs generates from docs/ and examples/.
export const collections = {
  docs: defineCollection({
    loader: glob({ pattern: '**/*.md', base: './src/content/docs' }),
    schema: z.object({
      title: z.string(),
      description: z.string().optional(),
      // The repository file the page is made from, for "Edit on GitHub".
      source: z.string().optional(),
    }),
  }),
};
