import { createReadStream, createWriteStream, promises as fs } from "node:fs";
import { once } from "node:events";
import path from "node:path";

const API = "https://goat.genomehubs.org/api/v2/searchPaginated";
const PAGE_SIZE = 8000;
const QUERY = "tax_rank(species)";
const RANKS = ["species", "genus", "family", "order", "class", "phylum", "kingdom", "domain"];
const FIELDS = [
  "assembly_level",
  "bioproject",
  "busco_completeness",
  "ebp_standard_criteria",
  "in_progress",
  "insdc_submitted",
  "published",
  "resampling_required",
  "sample_acquired",
  "sample_available",
  "sample_collected",
  "sequencing_status",
  "sequencing_status_ebp",
  "other_priority",
  "family_representative",
  "assembly_span",
];
const HEADERS = ["taxon_id", "taxon_rank", "scientific_name", ...RANKS, ...FIELDS];

const destination = process.argv[2];
if (!destination) {
  throw new Error("Usage: node scripts/download_goat_species.mjs /path/to/tol_species.tsv");
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function fetchJson(params, attempts = 8) {
  const url = new URL(API);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
  }

  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await fetch(url, {
        headers: { accept: "application/json", "accept-encoding": "gzip, br" },
        signal: AbortSignal.timeout(180_000),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
      const data = await response.json();
      if (!data?.status?.success) throw new Error(JSON.stringify(data?.status ?? data));
      return data;
    } catch (error) {
      lastError = error;
      if (attempt < attempts) await sleep(Math.min(30_000, 1000 * 2 ** (attempt - 1)));
    }
  }
  throw lastError;
}

function attributeValue(fields) {
  const candidates = [
    "attributes.keyword_value.raw",
    "attributes.keyword_value",
    "attributes.text_value.raw",
    "attributes.text_value",
    "attributes.long_value",
    "attributes.double_value",
    "attributes.date_value",
    "attributes.geo_point_value",
  ];
  for (const key of candidates) {
    if (fields[key] !== undefined) {
      const values = Array.isArray(fields[key]) ? fields[key] : [fields[key]];
      return values.map(String).join(";");
    }
  }
  return "";
}

function hitAttributes(hit) {
  const values = {};
  const innerHits = hit?.inner_hits?.attributes?.hits?.hits ?? [];
  for (const inner of innerHits) {
    const fields = inner.fields ?? {};
    const key = fields["attributes.key"]?.[0];
    if (key && FIELDS.includes(key)) values[key] = attributeValue(fields);
  }
  return values;
}

function tsvCell(value) {
  if (value === undefined || value === null || value === "") return "";
  const string = String(value);
  if (/^-?(?:\d+\.?\d*|\.\d+)$/.test(string)) return string;
  return `"${string.replaceAll('"', '""')}"`;
}

function tsvRow(values) {
  return `${values.map(tsvCell).join("\t")}\n`;
}

async function writeChunk(stream, chunk) {
  if (!stream.write(chunk)) await once(stream, "drain");
}

async function downloadMetadata() {
  const metadata = new Map();
  let searchAfter;
  let page = 0;
  while (true) {
    const data = await fetchJson({
      query: QUERY,
      result: "taxon",
      taxonomy: "ncbi",
      fields: FIELDS.join(","),
      includeEstimates: "true",
      limit: PAGE_SIZE,
      searchAfter: searchAfter ? JSON.stringify(searchAfter) : undefined,
    });
    for (const hit of data.hits) metadata.set(hit._source.taxon_id, hitAttributes(hit));
    page += 1;
    process.stderr.write(`metadata page ${page}: ${metadata.size} records\n`);
    if (!data.pagination.hasMore || data.hits.length === 0) break;
    searchAfter = data.pagination.searchAfter;
  }
  return metadata;
}

async function downloadRange({ index, start, end, metadata, partPath }) {
  const stream = createWriteStream(partPath, { encoding: "utf8" });
  let searchAfter = start ? [start] : undefined;
  let rows = 0;
  let pages = 0;

  try {
    while (true) {
      const data = await fetchJson({
        query: QUERY,
        result: "taxon",
        taxonomy: "ncbi",
        fields: "none",
        ranks: RANKS.join(","),
        limit: PAGE_SIZE,
        searchAfter: searchAfter ? JSON.stringify(searchAfter) : undefined,
      });
      if (data.hits.length === 0) break;

      let chunk = "";
      let reachedEnd = false;
      for (const hit of data.hits) {
        const source = hit._source;
        const taxonId = source.taxon_id;
        if (end && taxonId > end) {
          reachedEnd = true;
          break;
        }
        const lineage = Object.fromEntries(
          (source.lineage ?? []).map((node) => [node.taxon_rank, node.scientific_name]),
        );
        const attrs = metadata.get(taxonId) ?? {};
        chunk += tsvRow([
          taxonId,
          source.taxon_rank,
          source.scientific_name,
          ...RANKS.map((rank) => lineage[rank] ?? ""),
          ...FIELDS.map((field) => attrs[field] ?? ""),
        ]);
        rows += 1;
      }
      await writeChunk(stream, chunk);
      pages += 1;
      if (pages % 10 === 0 || reachedEnd) {
        process.stderr.write(`range ${index}: ${rows} rows (${pages} pages)\n`);
      }
      if (reachedEnd || !data.pagination.hasMore) break;
      searchAfter = data.pagination.searchAfter;
    }
  } finally {
    stream.end();
    await once(stream, "close");
  }
  return rows;
}

async function concatenate(parts, finalPath) {
  const output = createWriteStream(finalPath, { encoding: "utf8" });
  await writeChunk(output, tsvRow(HEADERS));
  for (const part of parts) {
    for await (const chunk of createReadStream(part)) await writeChunk(output, chunk);
  }
  output.end();
  await once(output, "close");
}

await fs.mkdir(path.dirname(destination), { recursive: true });
const metadata = await downloadMetadata();
process.stderr.write(`metadata complete: ${metadata.size} species\n`);

// NCBI taxon IDs are keyword-sorted by the API. Inclusive upper bounds plus
// search_after lower bounds avoid gaps at range boundaries.
const boundaries = [null, "14", "18", "22", null];
const ranges = boundaries.slice(0, -1).map((start, index) => ({
  index: index + 1,
  start,
  end: boundaries[index + 1],
  partPath: `${destination}.part-${index + 1}`,
  metadata,
}));

const counts = await Promise.all(ranges.map(downloadRange));
const total = counts.reduce((sum, count) => sum + count, 0);
if (total !== 2_004_790) {
  throw new Error(`Expected 2,004,790 species rows, downloaded ${total}`);
}

await concatenate(ranges.map((range) => range.partPath), destination);
for (const range of ranges) await fs.unlink(range.partPath);

const stat = await fs.stat(destination);
process.stdout.write(JSON.stringify({ destination, rows: total, bytes: stat.size, metadata: metadata.size }) + "\n");
