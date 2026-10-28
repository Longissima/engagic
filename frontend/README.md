# engagic frontend

The SvelteKit (Svelte 5) site at engagic.org. It is server-rendered on Cloudflare Workers through `@sveltejs/adapter-cloudflare`; see `wrangler.json` for routes and the R2 `engagic-tiles` binding that serves map tiles. Data comes from the engagic API (`src/lib/api/`).

```bash
npm install
npm run dev      # localhost:5173
npm run check    # svelte-check
npm run build
```

Test a change that touches `hooks.server.ts`, caching or anything that runs at the edge in workerd (`wrangler dev --local` on the Cloudflare build), not only under `vite dev`. Workers reject random values generated in global scope, and the adapter serves `caches.default` hits before `hooks.server.ts` runs, so a node-only test misses both.

Layout: `src/routes` (pages), `src/lib/components`, `src/lib/api` (typed API client), `src/lib/server` (server-only helpers), `src/lib/atlas` (map), `src/lib/utils` (including `attachments.ts`, which links page-span attachments to their pages).

Styling is hand-written CSS with design tokens (`app.css`); no CSS framework.

License: AGPL-3.0, as the parent project.
