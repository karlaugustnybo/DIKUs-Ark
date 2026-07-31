#!/usr/bin/env node

/**
 * Fetch authoritative GoaT lineage for TaxIDs absent from the public NCBI dump.
 * GoaT's current index can include newer ENA taxonomy records.
 */

import { promises as fs } from "node:fs";

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error(
    "Usage: node scripts/fetch_goat_lineages.mjs taxids.json output.json",
  );
}

const API = "https://goat.genomehubs.org/api/v2/search";
const BATCH_SIZE = Number(process.env.GOAT_BATCH_SIZE ?? 400);
const CONCURRENCY = 4;
const taxids = JSON.parse(await fs.readFile(inputPath, "utf8")).map(String);
const requestedTaxids = new Set(taxids);
const batches = [];
for (let index = 0; index < taxids.length; index += BATCH_SIZE) {
  batches.push(taxids.slice(index, index + BATCH_SIZE));
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function makeUrl(batch) {
  const query = `tax_eq(${batch.join(",")})`;
  const parameters = new URLSearchParams({
    query,
    result: "taxon",
    taxonomy: "ncbi",
    size: String(batch.length),
    offset: "0",
    fields: "none",
    includeEstimates: "true",
    emptyColumns: "true",
  });
  return `${API}?${parameters}`;
}

async function fetchBatch(batch) {
  let lastError;
  for (let attempt = 1; attempt <= 6; attempt += 1) {
    try {
      const response = await fetch(makeUrl(batch), {
        headers: {
          accept: "application/json",
          "user-agent": "Ark-IV-IUCN-GoaT-crosswalk/1.0",
        },
        signal: AbortSignal.timeout(120_000),
      });
      const text = await response.text();
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${text.slice(0, 300)}`);
      const parsed = JSON.parse(text);
      if (!parsed?.status?.success) {
        throw new Error(`GoaT query failed: ${JSON.stringify(parsed?.status)}`);
      }
      return parsed.results ?? [];
    } catch (error) {
      lastError = error;
      if (attempt < 6) await sleep(Math.min(20_000, 500 * 2 ** (attempt - 1)));
    }
  }
  throw lastError;
}

function summarize(result) {
  const taxon = result?.result ?? {};
  const ranked = {};
  for (const node of taxon.lineage ?? []) {
    if (
      ["species", "genus", "family", "order", "class", "phylum", "kingdom", "domain"].includes(
        node.taxon_rank,
      )
    ) {
      ranked[node.taxon_rank] ??= node.scientific_name;
    }
  }
  return {
    ncbi_taxid: String(taxon.taxon_id),
    scientific_name: taxon.scientific_name ?? null,
    taxon_rank: taxon.taxon_rank ?? null,
    species: ranked.species ?? null,
    genus: ranked.genus ?? null,
    family: ranked.family ?? null,
    order: ranked.order ?? null,
    class: ranked.class ?? null,
    phylum: ranked.phylum ?? null,
    kingdom: ranked.kingdom ?? null,
    domain: ranked.domain ?? null,
  };
}

const records = [];
let cursor = 0;
let completed = 0;

async function worker() {
  while (cursor < batches.length) {
    const index = cursor;
    cursor += 1;
    const results = await fetchBatch(batches[index]);
    records.push(...results.map(summarize));
    completed += 1;
    process.stderr.write(
      `GoaT lineage: ${completed}/${batches.length} batches, ${records.length} records\n`,
    );
  }
}

await Promise.all(Array.from({ length: CONCURRENCY }, () => worker()));
const byTaxid = new Map(
  records
    .filter((record) => requestedTaxids.has(record.ncbi_taxid))
    .map((record) => [record.ncbi_taxid, record]),
);
const missing = taxids.filter((taxid) => !byTaxid.has(taxid));

await fs.writeFile(
  outputPath,
  `${JSON.stringify(
    {
      generated_at: new Date().toISOString(),
      api: API,
      requested_taxids: taxids.length,
      returned_taxids: byTaxid.size,
      missing_taxids: missing,
      rows: [...byTaxid.values()].sort((a, b) =>
        a.ncbi_taxid.localeCompare(b.ncbi_taxid, "en", { numeric: true }),
      ),
    },
    null,
    2,
  )}\n`,
);

process.stdout.write(
  `${JSON.stringify({
    requested: taxids.length,
    returned: byTaxid.size,
    missing: missing.length,
    output: outputPath,
  })}\n`,
);
