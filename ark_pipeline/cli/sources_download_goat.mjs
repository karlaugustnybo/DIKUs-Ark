import { createHash } from "node:crypto";
import { createReadStream, existsSync, promises as fs, readFileSync } from "node:fs";
import path from "node:path";
import readline from "node:readline";

const API_BASE = process.env.GOAT_API_BASE ?? "https://goat.genomehubs.org/api/v3";
const SEARCH_API = new URL("search", `${API_BASE.replace(/\/$/, "")}/`);
const ROOT_TAXON_ID = String(process.env.GOAT_ROOT_TAXON_ID ?? "2759");
const PAGE_SIZE = Number(process.env.GOAT_PAGE_SIZE ?? 9990);
const MAX_ATTEMPTS = Number(process.env.GOAT_MAX_ATTEMPTS ?? 8);
const CHECKPOINT_VERSION = 4;
const RANKS = ["species", "genus", "family", "order", "class", "phylum", "kingdom", "domain"];
const FIELDS = [
  "assembly_level", "assembly_span", "bioproject", "busco_completeness", "chromosome_count",
  "chromosome_number", "contig_n50", "ebp_standard_criteria", "gene_count", "genome_size", "haploid_number", "in_progress",
  "insdc_submitted", "published", "resampling_required", "sample_acquired", "sample_available",
  "sample_collected", "sample_location", "scaffold_n50", "sequencing_status", "sequencing_status_ebp",
  "other_priority", "family_representative",
];
const REQUEST_FIELDS = FIELDS;
const HEADERS = ["taxon_id", "taxon_rank", "scientific_name", ...RANKS, ...FIELDS,
  "field_provenance_json"];

const args = process.argv.slice(2);
const restartIndex = args.indexOf("--restart");
const restart = restartIndex >= 0;
if (restart) args.splice(restartIndex, 1);
const destination = args[0];
if (!destination || args.length !== 1 || !Number.isInteger(PAGE_SIZE) || PAGE_SIZE < 1 || PAGE_SIZE > 9990 ||
    !Number.isInteger(MAX_ATTEMPTS) || MAX_ATTEMPTS < 1) {
  console.error("Usage: bun ark_pipeline/cli/sources_download_goat.mjs /path/to/tol_species_all_ranks.tsv [--restart]");
  process.exit(1);
}

const partPath = `${destination}.part`;
const checkpointPath = `${destination}.checkpoint.json`;
const lockPath = `${destination}.lock`;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function tsvCell(value) {
  if (value === undefined || value === null || value === "") return "";
  const string = String(value);
  if (/^-?(?:\d+\.?\d*|\.\d+)$/.test(string)) return string;
  return `"${string.replaceAll('"', '""')}"`;
}
const tsvRow = (values) => `${values.map(tsvCell).join("\t")}\n`;

function fieldValue(field) {
  const value = field && typeof field === "object" ? field.value : field;
  if (value === null || value === undefined) return "";
  return Array.isArray(value) ? value.map(String).join(";") : String(value);
}

function fieldProvenance(fields) {
  const provenance = {};
  for (const name of FIELDS) {
    const field = fields?.[name];
    if (!field || typeof field !== "object" || Array.isArray(field)) continue;
    const metadata = Object.fromEntries(Object.entries(field).filter(([key]) => key !== "value"));
    if (Object.keys(metadata).length) provenance[name] = metadata;
  }
  return Object.keys(provenance).length ? JSON.stringify(provenance) : "";
}

function processResult(item) {
  const result = item?.result;
  const taxonId = String(result?.taxon_id ?? "");
  // GOAT supplements NCBI with ENA placeholder taxa whose IDs are accession-like
  // strings (for example bCurMin), so taxon IDs are intentionally not numeric-only.
  if (!taxonId || /[\t\r\n]/.test(taxonId) || !result?.taxon_rank || !result?.scientific_name) {
    throw new Error(`GOAT returned incomplete taxon data: ${JSON.stringify(result)}`);
  }
  const lineage = new Map();
  for (const node of result.lineage ?? []) {
    if (node?.taxon_rank && node?.scientific_name && !lineage.has(node.taxon_rank)) {
      lineage.set(node.taxon_rank, node.scientific_name);
    }
  }
  return {
    taxonId,
    row: tsvRow([taxonId, result.taxon_rank, result.scientific_name,
      ...RANKS.map((rank) => lineage.get(rank) ?? ""),
      ...FIELDS.map((field) => fieldValue(result.fields?.[field])),
      fieldProvenance(result.fields)]),
  };
}

function requestBody(size, searchAfter) {
  const query = { index: "taxon", taxa: [ROOT_TAXON_ID], taxon_filter_type: "tree",
    fields: REQUEST_FIELDS.map((name) => ({ name })) };
  const params = { size, include_estimates: true, include_lineage: true, include_taxon_names: false,
    sort_by: "taxon_id", sort_order: "asc", taxonomy: "ncbi" };
  if (searchAfter !== undefined) params.search_after = searchAfter;
  return JSON.stringify({ query_yaml: JSON.stringify(query), params_yaml: JSON.stringify(params) });
}

function retryDelay(response, attempt) {
  const retryAfter = response?.headers?.get("retry-after");
  if (retryAfter && /^\d+$/.test(retryAfter)) return Number(retryAfter) * 1000;
  const exponential = Math.min(60_000, 1000 * 2 ** (attempt - 1));
  return exponential + Math.floor(Math.random() * Math.min(1000, exponential / 4));
}

async function search(size, searchAfter) {
  let lastError;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
    let response;
    try {
      response = await fetch(SEARCH_API, { method: "POST",
        headers: { accept: "application/json", "content-type": "application/json", "accept-encoding": "gzip, br" },
        body: requestBody(size, searchAfter), signal: AbortSignal.timeout(180_000) });
      if (!response.ok) {
        const body = (await response.text()).slice(0, 500);
        const error = new Error(`HTTP ${response.status}: ${body}`);
        if (response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429) error.nonRetryable = true;
        throw error;
      }
      const data = await response.json();
      if (!data?.status?.success) throw new Error(`GOAT search failed: ${JSON.stringify(data?.status ?? data)}`);
      return data;
    } catch (error) {
      lastError = error;
      if (error.nonRetryable || attempt === MAX_ATTEMPTS) break;
      const delay = retryDelay(response, attempt);
      process.stderr.write(`Request failed (${attempt}/${MAX_ATTEMPTS}); retrying in ${(delay / 1000).toFixed(1)}s: ${String(error.message ?? error).slice(0, 180)}\n`);
      await sleep(delay);
    }
  }
  throw lastError;
}

const fingerprint = () => createHash("sha256").update(JSON.stringify({
  apiBase: API_BASE, root: ROOT_TAXON_ID, pageSize: PAGE_SIZE, ranks: RANKS, fields: REQUEST_FIELDS,
})).digest("hex");

async function acquireLock() {
  try {
    const lock = await fs.open(lockPath, "wx");
    await lock.writeFile(`${JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() })}\n`);
    await lock.close();
    return;
  } catch (error) { if (error.code !== "EEXIST") throw error; }
  let stale = false;
  try {
    const data = JSON.parse(readFileSync(lockPath, "utf8"));
    if (!Number.isInteger(data.pid)) stale = true;
    else try { process.kill(data.pid, 0); } catch (error) { if (error.code === "ESRCH") stale = true; else throw error; }
  } catch { stale = true; }
  if (!stale) throw new Error(`Another downloader owns ${lockPath}`);
  await fs.unlink(lockPath).catch(() => {});
  return acquireLock();
}

async function saveCheckpoint(checkpoint) {
  const tmp = `${checkpointPath}.tmp`;
  await fs.writeFile(tmp, `${JSON.stringify(checkpoint)}\n`);
  await fs.rename(tmp, checkpointPath);
}

async function getExpectedRows() {
  const data = await search(1);
  const hits = Number(data.status.hits);
  if (!Number.isSafeInteger(hits) || hits < 1) throw new Error(`Invalid GOAT count: ${JSON.stringify(data.status)}`);
  return hits;
}

async function initialize(expected, output) {
  const currentFingerprint = fingerprint();
  if (existsSync(checkpointPath)) {
    const checkpoint = JSON.parse(readFileSync(checkpointPath, "utf8"));
    if (checkpoint.version !== CHECKPOINT_VERSION || checkpoint.fingerprint !== currentFingerprint)
      throw new Error("Checkpoint is incompatible. Re-run with --restart.");
    if (checkpoint.expectedRows !== expected)
      throw new Error(`GOAT changed (${checkpoint.expectedRows} -> ${expected}). Re-run with --restart.`);
    const stat = await fs.stat(partPath).catch(() => null);
    if (!stat || stat.size < checkpoint.committedBytes)
      throw new Error("Partial output is missing or shorter than its checkpoint. Re-run with --restart.");
    await output.truncate(checkpoint.committedBytes);
    return checkpoint;
  }
  const header = Buffer.from(tsvRow(HEADERS));
  await output.write(header, 0, header.length, 0);
  await output.sync();
  const checkpoint = { version: CHECKPOINT_VERSION, fingerprint: currentFingerprint, expectedRows: expected,
    rows: 0, pages: 0, committedBytes: header.length, updatedAt: new Date().toISOString() };
  await saveCheckpoint(checkpoint);
  return checkpoint;
}

async function writeFully(handle, buffer, position) {
  let offset = 0;
  while (offset < buffer.length) {
    const { bytesWritten } = await handle.write(buffer, offset, buffer.length - offset, position + offset);
    if (bytesWritten < 1) throw new Error("Output write made no progress");
    offset += bytesWritten;
  }
  return position + buffer.length;
}

async function loadCommittedIds(expectedRows) {
  const ids = new Set();
  const lines = readline.createInterface({
    input: createReadStream(partPath, { encoding: "utf8" }),
    crlfDelay: Infinity,
  });
  let header = true;
  for await (const line of lines) {
    if (header) { header = false; continue; }
    if (!line) continue;
    const tab = line.indexOf("\t");
    let taxonId = tab < 0 ? line : line.slice(0, tab);
    if (taxonId.startsWith('"') && taxonId.endsWith('"')) {
      taxonId = taxonId.slice(1, -1).replaceAll('""', '"');
    }
    if (ids.has(taxonId)) throw new Error(`Duplicate taxon ${taxonId} in committed output`);
    ids.add(taxonId);
  }
  if (ids.size !== expectedRows) {
    throw new Error(`Committed output has ${ids.size} unique IDs; checkpoint records ${expectedRows} rows`);
  }
  return ids;
}

await fs.mkdir(path.dirname(path.resolve(destination)), { recursive: true });
await acquireLock();
let completed = false;
const started = Date.now();
try {
  const expected = await getExpectedRows();
  if (restart) {
    await fs.unlink(partPath).catch(() => {});
    await fs.unlink(checkpointPath).catch(() => {});
  }
  if (!existsSync(checkpointPath) && existsSync(partPath)) {
    throw new Error("Partial output exists without a checkpoint. Re-run with --restart.");
  }
  const output = await fs.open(partPath, existsSync(partPath) ? "r+" : "w+");
  try {
    const checkpoint = await initialize(expected, output);
    const ids = await loadCommittedIds(checkpoint.rows);
    process.stderr.write(`GOAT reports ${expected.toLocaleString()} taxa under ${ROOT_TAXON_ID}; ${FIELDS.length} output attributes plus estimated ${COVERAGE_FIELD}; page size ${PAGE_SIZE.toLocaleString()}.\n`);
    if (checkpoint.rows) process.stderr.write(`Resuming after ${checkpoint.rows.toLocaleString()} rows (${checkpoint.pages} pages).\n`);
    while (checkpoint.rows < expected) {
      const data = await search(PAGE_SIZE, checkpoint.searchAfter);
      const hits = Number(data.status.hits);
      const results = data.results ?? [];
      if (hits !== expected) throw new Error(`GOAT total changed during the run (${expected} -> ${hits})`);
      if (results.length < 1 || results.length > PAGE_SIZE) throw new Error(`Invalid page length ${results.length}`);
      let chunk = "";
      for (const item of results) {
        const processed = processResult(item);
        if (ids.has(processed.taxonId)) throw new Error(`GOAT returned duplicate taxon ${processed.taxonId}`);
        ids.add(processed.taxonId);
        chunk += processed.row;
      }
      if (checkpoint.rows + results.length > expected) throw new Error("GOAT returned too many rows");
      if (checkpoint.rows + results.length < expected && !Array.isArray(data.search_after)) throw new Error("GOAT omitted the next cursor");
      checkpoint.committedBytes = await writeFully(output, Buffer.from(chunk), checkpoint.committedBytes);
      await output.sync();
      checkpoint.rows += results.length;
      checkpoint.pages += 1;
      checkpoint.searchAfter = data.search_after;
      checkpoint.updatedAt = new Date().toISOString();
      await saveCheckpoint(checkpoint);
      const elapsed = (Date.now() - started) / 1000;
      const rate = checkpoint.rows / Math.max(elapsed, 0.001);
      const eta = (expected - checkpoint.rows) / rate;
      process.stderr.write(`Page ${checkpoint.pages}: ${checkpoint.rows.toLocaleString()}/${expected.toLocaleString()} rows (${rate.toFixed(0)}/s, ETA ${(eta / 60).toFixed(1)} min)\n`);
    }
    if (checkpoint.rows !== expected) throw new Error(`Downloaded ${checkpoint.rows}; expected ${expected}`);
    if (ids.size !== expected) throw new Error(`Downloaded ${ids.size} unique IDs; expected ${expected}`);
    const finalExpected = await getExpectedRows();
    if (finalExpected !== expected) throw new Error(`GOAT changed during the run (${expected} -> ${finalExpected})`);
    await output.sync();
  } finally { await output.close(); }
  await fs.rename(partPath, destination);
  await fs.unlink(checkpointPath).catch(() => {});
  const stat = await fs.stat(destination);
  completed = true;
  process.stdout.write(`${JSON.stringify({ destination, rows: expected, bytes: stat.size,
    pages: Math.ceil(expected / PAGE_SIZE), fields: FIELDS.length, estimates: true,
    elapsedSeconds: (Date.now() - started) / 1000 })}\n`);
} finally {
  await fs.unlink(lockPath).catch(() => {});
  if (!completed) process.stderr.write(`Download stopped safely; committed pages remain in ${partPath}.\n`);
}
