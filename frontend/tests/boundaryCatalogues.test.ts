// @ts-expect-error -- Bun's test types are not part of the browser bundle
import { expect, test } from 'bun:test';

let sequence = 0;
async function freshStore() {
  const path = `../src/lib/stores/boundaries.ts?test=${++sequence}`;
  return import(path);
}

const preview = {
  id: 'municipality', name: 'Preview', short_name: 'Local areas', status: 'ready',
  catalog_url: '/preview.json', group: 'Political', relationship: 'single'
};
const global = {
  ...preview, name: 'Global ADM2', available_parent_codes: ['DNK', 'USA'],
  catalog_partition_url: '/data/adm2-catalogs/{parent}.json',
  catalog_url: '/data/adm2-catalogs/all.json'
};

test('global installation fetches only selected country names and caches them', async () => {
  const original = globalThis.fetch;
  const requests: string[] = [];
  globalThis.fetch = (async (url: string) => {
    requests.push(url);
    if (url.endsWith('boundary-frameworks.json')) return Response.json({ frameworks: [preview] });
    if (url.endsWith('framework.json')) return Response.json(global);
    if (url.endsWith('/usa.json')) return Response.json({ features: [
      { code: 'county-1', name: 'County', parent_code: 'USA', boundary_type: 'ADM2' }
    ] });
    throw new Error(`Unexpected geometry or worldwide request: ${url}`);
  }) as typeof fetch;
  try {
    const store = await freshStore();
    expect((await store.loadBoundaryFrameworks())[0].name).toBe('Global ADM2');
    const result = await store.loadBoundaryCollection('municipality', ['USA']);
    expect(result.features[0].geometry).toBeNull();
    await store.loadBoundaryCollection('municipality', ['USA']);
    expect(requests.filter((url) => url.endsWith('/usa.json')).length).toBe(1);
    expect(requests.some((url) => url.endsWith('/all.json'))).toBe(false);
  } finally {
    globalThis.fetch = original;
  }
});

for (const missing of ['404', 'spa-html']) {
  test(`a code-only checkout keeps the preview when optional ADM2 assets return ${missing}`, async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (async (url: string) => url.endsWith('boundary-frameworks.json')
      ? Response.json({ frameworks: [preview] })
      : missing === '404' ? new Response('', { status: 404 })
        : new Response('<html>app</html>', { headers: { 'content-type': 'text/html' } })) as typeof fetch;
    try {
      const store = await freshStore();
      expect((await store.loadBoundaryFrameworks())[0].name).toBe('Preview');
    } finally {
      globalThis.fetch = original;
    }
  });
}
