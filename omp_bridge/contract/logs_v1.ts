/** Strict JSON v1 important-log contracts mirrored by workbench.contracts.logs_v1. */

import { createHash } from "node:crypto";

export const LOG_SCHEMA_VERSION = 1 as const;
export const MAX_BODY_BYTES = 1_000_000;
export const MAX_SUMMARY_RESULT_BYTES = 8_192;

export type SubjectRef = { schemaVersion: 1; kind: "task" | "command"; id: string };
export type SourceRef = { schemaVersion: 1; sourceKind: "terminal" | "task_spec" | "manager_instruction" | "worker_instruction" | "report" | "decision" | "event"; collectorEpoch: number; taskId?: string; revision?: number; runId?: string; commandId?: string; messageId?: string; eventId?: string };
export type SourceRange = { schemaVersion: 1; source: SourceRef; start: number; end: number; digest: string };
export type SourceGap = { schemaVersion: 1; source: SourceRef; start: number; end: number; reason: "collector_gap" | "storage_gap" | "evicted" | "unknown" };
export type ObservationBoundary = { schemaVersion: 1; eventId: string; source: SourceRef; kind: "start" | "final_drain"; offset: number; observedAt: string };
export type ObservationCoverage = { schemaVersion: 1; coverageId: string; source: SourceRef; startEventId: string; finalEventId: string; finalOffset: number; sourceIds: string[] };
export type RawChunk = { schemaVersion: 1; range: SourceRange; dataBase64: string };
export type RawReadResult = { schemaVersion: 1; source: SourceRef; status: "available" | "partial" | "unavailable" | "unknown"; chunks: RawChunk[]; available: SourceRange[]; missing: SourceGap[] };
export type ImportantBody = { schemaVersion: 1; facts: string[]; interpretations: string[]; unknowns: string[] };
export type ImportantEntry = { schemaVersion: 1; entryId: string; subject: SubjectRef; kind: "start" | "event" | "cumulative" | "intermediate" | "gap" | "closure"; sources: SourceRange[]; sourceGaps: SourceGap[]; recordedAt: string; body: ImportantBody; bodyCodec: "identity-v1" | "zlib-v1"; decodedBytes: number; storedBytes: number; bodySha256: string; originDbUuid: string };
export type SubjectStat = { schemaVersion: 1; subject: SubjectRef; entryCount: number; storedBytes: number };
export type HealthSnapshot = { schemaVersion: 1; dbUuid: string; measuredAt: string; stale: boolean; mainBytes: number; walBytes: number; shmBytes: number; totalBytes: number; logicalLiveBytes: number; freelistBytes: number; subjectStats: SubjectStat[]; cleanupCandidates: number | null; lastWrite: string | null; lastLock: string | null; lastMaintenance: string | null; diskFreeBytes: number | null; lastQuickCheck: string | null; metadataOutsideBudget: true };
export type SummaryRequest = { schemaVersion: 1; requestId: string; subject: SubjectRef; revision: number | null; authorizationRef: string; authorizationEpoch: number; inputKey: string; skillName: string; skillDigest: string; sources: SourceRange[]; sourceGaps: SourceGap[]; priorCumulativeEntryId: string | null; dbHealth: HealthSnapshot; requestedAt: string };
export type SummaryUsage = { schemaVersion: 1; inputTokens: number | null; outputTokens: number | null; costUsd: string | null };
export type SummaryResult = { schemaVersion: 1; requestId: string; inputKey: string; skillDigest: string; sources: SourceRange[]; facts: string[]; interpretations: string[]; unknowns: string[]; usage: SummaryUsage };

export interface RawReader { readRaw(source: SourceRef, start: number, end: number): Promise<RawReadResult> }
export interface SubjectEventSink { recordSubjectEvent(subject: SubjectRef, eventId: string, source: SourceRef): Promise<void> }
export interface ImportantStorePort { putEntry(entry: ImportantEntry): Promise<ImportantEntry>; getEntry(entryId: string): Promise<ImportantEntry | null>; listEntries(subject: SubjectRef, limit: number, after?: string): Promise<ImportantEntry[]>; healthSnapshot(): Promise<HealthSnapshot> }
export interface SummaryAdapter { dispatchSummary(request: SummaryRequest): Promise<SummaryResult> }
export interface RequestJournal { reserveSummaryRequest(request: SummaryRequest): Promise<void>; recordSummaryState(requestId: string, state: string, detail?: string): Promise<void>; recordSummaryResultReceipt(result: SummaryResult, entryId: string): Promise<boolean>; getSummaryRequest(requestId: string): Promise<unknown>; listUnresolvedSummaryRequests(limit: number, after?: string): Promise<unknown[]> }
export interface ObservationJournal { recordObservationBoundary(boundary: ObservationBoundary): Promise<string>; recordGapfreeCoverage(coverage: ObservationCoverage): Promise<string> }

export class LogContractError extends Error { readonly code = "invalid_log_contract_v1" as const; }
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const digest = /^[0-9a-f]{64}$/;
const utc = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/;
const sourceKinds = new Set(["terminal", "task_spec", "manager_instruction", "worker_instruction", "report", "decision", "event"]);
const entryKinds = new Set(["start", "event", "cumulative", "intermediate", "gap", "closure"]);
const gapReasons = new Set(["collector_gap", "storage_gap", "evicted", "unknown"]);

function record(value: unknown, name: string, required: string[], optional: string[] = []): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value) ||
      (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null)) {
    throw new LogContractError(`${name} must be a plain object`);
  }
  const data = value as Record<string, unknown>;
  const allowed = new Set([...required, ...optional]);
  for (const key of required) if (!Object.hasOwn(data, key)) throw new LogContractError(`${name} missing ${key}`);
  for (const key of Object.keys(data)) if (!allowed.has(key)) throw new LogContractError(`${name} unsupported ${key}`);
  if (data.schemaVersion !== LOG_SCHEMA_VERSION) throw new LogContractError(`${name} unsupported schemaVersion`);
  return data;
}

function id(value: unknown, name: string): string {
  if (typeof value !== "string" || !uuid.test(value)) throw new LogContractError(`${name} must be canonical UUID`);
  return value;
}
function sha(value: unknown, name: string): string {
  if (typeof value !== "string" || !digest.test(value)) throw new LogContractError(`${name} must be lowercase SHA-256 hex`);
  return value;
}
function integer(value: unknown, name: string, minimum = 0): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) throw new LogContractError(`${name} must be safe integer >= ${minimum}`);
  return value;
}
function text(value: unknown, name: string, maximum = 4096): string {
  if (typeof value !== "string" || !value || !validUnicode(value) || Buffer.byteLength(value, "utf8") > maximum) throw new LogContractError(`${name} must be bounded text`);
  return value;
}
function validUnicode(value: string): boolean {
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      if (++i >= value.length || value.charCodeAt(i) < 0xdc00 || value.charCodeAt(i) > 0xdfff) return false;
    } else if (code >= 0xdc00 && code <= 0xdfff) return false;
  }
  return true;
}
function timestamp(value: unknown, name: string): string {
  if (typeof value !== "string" || !utc.test(value) || value.startsWith("0000-") || !Number.isFinite(Date.parse(value)) ||
      new Date(value).toISOString().slice(0, 19) !== value.slice(0, 19)) throw new LogContractError(`${name} must be valid UTC timestamp`);
  return value;
}
function array(value: unknown, name: string, maximum = 1024): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) throw new LogContractError(`${name} must be bounded array`);
  return value;
}
function strings(value: unknown, name: string): string[] {
  return array(value, name, 128).map((item) => text(item, name, MAX_BODY_BYTES));
}
function sorted(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sorted);
  if (value && typeof value === "object") {
    if (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null) throw new LogContractError("JSON value must be a plain object");
    const data = value as Record<string, unknown>;
    const result: Record<string, unknown> = {};
    for (const key of Object.keys(data).sort()) {
      if (!validUnicode(key)) throw new LogContractError("JSON key must be valid Unicode");
      result[key] = sorted(data[key]);
    }
    return result;
  }
  if (typeof value === "number" && !Number.isSafeInteger(value)) throw new LogContractError("invalid shared JSON number");
  if (typeof value === "string" && !validUnicode(value)) throw new LogContractError("JSON string must be valid Unicode");
  if (value === undefined || typeof value === "function" || typeof value === "bigint") throw new LogContractError("invalid JSON value");
  return value;
}
export function canonicalJson(value: unknown): string { return JSON.stringify(sorted(value)); }
export function decodeJson(raw: string, maxBytes = MAX_BODY_BYTES): unknown {
  if (Buffer.byteLength(raw, "utf8") > maxBytes) throw new LogContractError("JSON envelope exceeds bound");
  let cursor = 0;
  const space = () => { while (/\s/.test(raw[cursor] ?? "") && cursor < raw.length) cursor++; };
  const stringToken = (): string => {
    const start = cursor++;
    while (cursor < raw.length) {
      if (raw[cursor] === "\\") { cursor += 2; continue; }
      if (raw[cursor++] === '"') {
        try { return JSON.parse(raw.slice(start, cursor)) as string; }
        catch { throw new LogContractError("invalid JSON string"); }
      }
    }
    throw new LogContractError("unterminated JSON string");
  };
  const valueToken = (): void => {
    space();
    const ch = raw[cursor];
    if (ch === "{") {
      cursor++; space(); const keys = new Set<string>();
      if (raw[cursor] === "}") { cursor++; return; }
      while (cursor < raw.length) {
        space(); if (raw[cursor] !== '"') throw new LogContractError("JSON object key expected");
        const key = stringToken();
        if (keys.has(key)) throw new LogContractError(`duplicate JSON field: ${key}`);
        keys.add(key); space();
        if (raw[cursor++] !== ":") throw new LogContractError("JSON colon expected");
        valueToken(); space();
        const next = raw[cursor++];
        if (next === "}") return;
        if (next !== ",") throw new LogContractError("JSON object separator expected");
      }
    } else if (ch === "[") {
      cursor++; space();
      if (raw[cursor] === "]") { cursor++; return; }
      while (cursor < raw.length) {
        valueToken(); space();
        const next = raw[cursor++];
        if (next === "]") return;
        if (next !== ",") throw new LogContractError("JSON array separator expected");
      }
    } else if (ch === '"') {
      stringToken();
    } else {
      const pattern = /(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/y;
      pattern.lastIndex = cursor;
      const token = pattern.exec(raw);
      if (!token) throw new LogContractError("invalid JSON token");
      if (/[.eE]/.test(token[0])) throw new LogContractError("non-integer JSON number");
      cursor = pattern.lastIndex;
    }
  };
  valueToken(); space();
  if (cursor !== raw.length) throw new LogContractError("trailing JSON content");
  let value: unknown;
  try { value = JSON.parse(raw) as unknown; }
  catch { throw new LogContractError("invalid JSON envelope"); }
  canonicalJson(value);
  return value;
}

export function parseSubjectRef(value: unknown): SubjectRef {
  const d = record(value, "SubjectRef", ["schemaVersion", "kind", "id"]);
  if (d.kind !== "task" && d.kind !== "command") throw new LogContractError("unsupported subject kind");
  id(d.id, "subject.id"); return d as unknown as SubjectRef;
}
export function parseSourceRef(value: unknown): SourceRef {
  const d = record(value, "SourceRef", ["schemaVersion", "sourceKind", "collectorEpoch"], ["taskId", "revision", "runId", "commandId", "messageId", "eventId"]);
  if (!sourceKinds.has(d.sourceKind as string)) throw new LogContractError("unsupported source kind");
  integer(d.collectorEpoch, "collectorEpoch", 1);
  for (const key of ["taskId", "runId", "commandId", "messageId", "eventId"]) if (d[key] !== undefined) id(d[key], key);
  if ((d.taskId === undefined) !== (d.revision === undefined)) throw new LogContractError("taskId and revision must appear together");
  if (d.runId !== undefined && d.taskId === undefined) throw new LogContractError("runId requires its Task and revision");
  if (d.revision !== undefined) integer(d.revision, "revision", 1);
  if (d.sourceKind === "terminal") {
    if ((d.runId === undefined) === (d.commandId === undefined) || d.messageId !== undefined || d.eventId !== undefined)
      throw new LogContractError("terminal source requires exactly one run or command ID without aliases");
  } else if ((d.messageId === undefined) === (d.eventId === undefined)) {
    throw new LogContractError("structured source requires exactly one message or event ID");
  }
  return d as unknown as SourceRef;
}
export function parseSourceRange(value: unknown): SourceRange {
  const d = record(value, "SourceRange", ["schemaVersion", "source", "start", "end", "digest"]);
  parseSourceRef(d.source); const start = integer(d.start, "start"); const end = integer(d.end, "end");
  if (end <= start) throw new LogContractError("SourceRange must be nonempty");
  sha(d.digest, "range digest"); return d as unknown as SourceRange;
}
export function parseSourceGap(value: unknown): SourceGap {
  const d = record(value, "SourceGap", ["schemaVersion", "source", "start", "end", "reason"]);
  parseSourceRef(d.source); const start = integer(d.start, "start"); const end = integer(d.end, "end");
  if (end <= start || !gapReasons.has(d.reason as string)) throw new LogContractError("invalid source gap");
  return d as unknown as SourceGap;
}
export function parseObservationBoundary(value: unknown): ObservationBoundary {
  const d = record(value, "ObservationBoundary", ["schemaVersion", "eventId", "source", "kind", "offset", "observedAt"]);
  id(d.eventId, "eventId"); const source = parseSourceRef(d.source);
  if (source.sourceKind !== "terminal" || source.runId === undefined) throw new LogContractError("observation boundary requires a terminal Run");
  if (d.kind !== "start" && d.kind !== "final_drain") throw new LogContractError("invalid observation boundary kind");
  integer(d.offset, "offset");
  if (d.kind === "start" && d.offset !== 0) throw new LogContractError("start boundary must have offset zero");
  timestamp(d.observedAt, "observedAt"); return d as unknown as ObservationBoundary;
}
export function parseObservationCoverage(value: unknown): ObservationCoverage {
  const d = record(value, "ObservationCoverage", ["schemaVersion", "coverageId", "source", "startEventId", "finalEventId", "finalOffset", "sourceIds"]);
  for (const key of ["coverageId", "startEventId", "finalEventId"]) id(d[key], key);
  if (d.startEventId === d.finalEventId) throw new LogContractError("coverage boundaries must be distinct");
  const source = parseSourceRef(d.source);
  if (source.sourceKind !== "terminal" || source.runId === undefined) throw new LogContractError("observation coverage requires a terminal Run");
  integer(d.finalOffset, "finalOffset");
  const ids = array(d.sourceIds, "sourceIds").map((item) => id(item, "sourceId"));
  if (new Set(ids).size !== ids.length) throw new LogContractError("duplicate coverage source ID");
  return d as unknown as ObservationCoverage;
}
function physicalSourceKey(source: SourceRef): string {
  const stable: Record<string, unknown> = {sourceKind: source.sourceKind};
  const key = source.sourceKind === "terminal"
    ? (source.runId !== undefined ? "runId" : "commandId")
    : (source.messageId !== undefined ? "messageId" : "eventId");
  stable[key] = source[key];
  return canonicalJson(stable);
}
function sourceBelongs(subject: SubjectRef, source: SourceRef, revision?: number | null): void {
  if (subject.kind === "task") {
    if (source.taskId !== subject.id) throw new LogContractError("source belongs to another Task");
    if (revision !== undefined && revision !== null && source.revision !== revision) throw new LogContractError("source Task revision mismatch");
  } else if (source.commandId !== subject.id || source.taskId !== undefined) {
    throw new LogContractError("command subject requires unlinked command source");
  }
}
export function parseRawReadResult(value: unknown): RawReadResult {
  const d = record(value, "RawReadResult", ["schemaVersion", "source", "status", "chunks", "available", "missing"]);
  const source = parseSourceRef(d.source);
  if (!["available", "partial", "unavailable", "unknown"].includes(d.status as string)) throw new LogContractError("invalid raw status");
  for (const item of array(d.chunks, "chunks", 32)) {
    const c = record(item, "RawChunk", ["schemaVersion", "range", "dataBase64"]);
    const span = parseSourceRange(c.range);
    if (canonicalJson(span.source) !== canonicalJson(source) || typeof c.dataBase64 !== "string" || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(c.dataBase64)) throw new LogContractError("raw chunk source/base64 mismatch");
    const bytes = Buffer.from(c.dataBase64, "base64");
    if (bytes.toString("base64") !== c.dataBase64 || bytes.length !== span.end - span.start || createHash("sha256").update(bytes).digest("hex") !== span.digest) throw new LogContractError("raw chunk length/checksum mismatch");
  }
  for (const item of array(d.available, "available", 32)) if (canonicalJson(parseSourceRange(item).source) !== canonicalJson(source)) throw new LogContractError("available source mismatch");
  for (const item of array(d.missing, "missing", 32)) if (canonicalJson(parseSourceGap(item).source) !== canonicalJson(source)) throw new LogContractError("missing source mismatch");
  if (Buffer.byteLength(canonicalJson(d), "utf8") > MAX_BODY_BYTES) throw new LogContractError("raw result exceeds bound");
  return d as unknown as RawReadResult;
}
export function parseImportantBody(value: unknown): ImportantBody {
  const d = record(value, "ImportantBody", ["schemaVersion", "facts", "interpretations", "unknowns"]);
  for (const key of ["facts", "interpretations", "unknowns"]) strings(d[key], key);
  if (Buffer.byteLength(canonicalJson(d), "utf8") > MAX_BODY_BYTES) throw new LogContractError("important body exceeds bound");
  return d as unknown as ImportantBody;
}
export function parseImportantEntry(value: unknown): ImportantEntry {
  const d = record(value, "ImportantEntry", ["schemaVersion", "entryId", "subject", "kind", "sources", "sourceGaps", "recordedAt", "body", "bodyCodec", "decodedBytes", "storedBytes", "bodySha256", "originDbUuid"]);
  id(d.entryId, "entryId"); const subject = parseSubjectRef(d.subject);
  if (!entryKinds.has(d.kind as string)) throw new LogContractError("invalid entry kind");
  array(d.sources, "sources", 128).forEach((item) => sourceBelongs(subject, parseSourceRange(item).source));
  array(d.sourceGaps, "sourceGaps", 128).forEach((item) => sourceBelongs(subject, parseSourceGap(item).source));
  if (Buffer.byteLength(canonicalJson(d.sources), "utf8") + Buffer.byteLength(canonicalJson(d.sourceGaps), "utf8") > MAX_BODY_BYTES) throw new LogContractError("entry source metadata exceeds bound");
  timestamp(d.recordedAt, "recordedAt");
  const body = parseImportantBody(d.body); const bytes = Buffer.from(canonicalJson(body), "utf8");
  if (d.bodyCodec !== "identity-v1" && d.bodyCodec !== "zlib-v1") throw new LogContractError("invalid body codec");
  if (integer(d.decodedBytes, "decodedBytes") !== bytes.length || integer(d.storedBytes, "storedBytes", 1) > MAX_BODY_BYTES) throw new LogContractError("entry byte count mismatch");
  if (sha(d.bodySha256, "bodySha256") !== createHash("sha256").update(bytes).digest("hex")) throw new LogContractError("entry checksum mismatch");
  id(d.originDbUuid, "originDbUuid"); return d as unknown as ImportantEntry;
}
export function parseHealthSnapshot(value: unknown): HealthSnapshot {
  const d = record(value, "HealthSnapshot", ["schemaVersion", "dbUuid", "measuredAt", "stale", "mainBytes", "walBytes", "shmBytes", "totalBytes", "logicalLiveBytes", "freelistBytes", "subjectStats", "cleanupCandidates", "lastWrite", "lastLock", "lastMaintenance", "diskFreeBytes", "lastQuickCheck", "metadataOutsideBudget"]);
  id(d.dbUuid, "dbUuid"); timestamp(d.measuredAt, "measuredAt");
  if (typeof d.stale !== "boolean" || d.metadataOutsideBudget !== true) throw new LogContractError("invalid health flags");
  for (const key of ["mainBytes", "walBytes", "shmBytes", "totalBytes", "logicalLiveBytes", "freelistBytes"]) integer(d[key], key);
  if (d.cleanupCandidates !== null) integer(d.cleanupCandidates, "cleanupCandidates");
  if (d.totalBytes !== (d.mainBytes as number) + (d.walBytes as number) + (d.shmBytes as number)) throw new LogContractError("physical sum mismatch");
  for (const item of array(d.subjectStats, "subjectStats")) {
    const stat = record(item, "SubjectStat", ["schemaVersion", "subject", "entryCount", "storedBytes"]);
    parseSubjectRef(stat.subject); integer(stat.entryCount, "entryCount"); integer(stat.storedBytes, "storedBytes");
  }
  for (const key of ["lastWrite", "lastLock", "lastMaintenance", "lastQuickCheck"]) if (d[key] !== null) text(d[key], key, 256);
  if (d.diskFreeBytes !== null) integer(d.diskFreeBytes, "diskFreeBytes");
  return d as unknown as HealthSnapshot;
}
export function summaryInputKey(subject: SubjectRef, revision: number | null, sources: SourceRange[], gaps: SourceGap[], skillDigest: string): string {
  parseSubjectRef(subject); sha(skillDigest, "skillDigest"); sources.forEach(parseSourceRange); gaps.forEach(parseSourceGap);
  return createHash("sha256").update(canonicalJson({ subject, revision, sources, sourceGaps: gaps, skillDigest }), "utf8").digest("hex");
}
export function parseSummaryRequest(value: unknown): SummaryRequest {
  const d = record(value, "SummaryRequest", ["schemaVersion", "requestId", "subject", "revision", "authorizationRef", "authorizationEpoch", "inputKey", "skillName", "skillDigest", "sources", "sourceGaps", "priorCumulativeEntryId", "dbHealth", "requestedAt"]);
  id(d.requestId, "requestId"); const subject = parseSubjectRef(d.subject);
  if (subject.kind === "task") integer(d.revision, "revision", 1);
  else if (d.revision !== null) throw new LogContractError("command revision must be null");
  id(d.authorizationRef, "authorizationRef"); integer(d.authorizationEpoch, "authorizationEpoch", 1);
  sha(d.inputKey, "inputKey"); text(d.skillName, "skillName", 128); sha(d.skillDigest, "skillDigest");
  const sources = array(d.sources, "sources", 128).map(parseSourceRange);
  const gaps = array(d.sourceGaps, "sourceGaps", 128).map(parseSourceGap);
  sources.forEach((span) => sourceBelongs(subject, span.source, d.revision as number | null));
  gaps.forEach((gap) => sourceBelongs(subject, gap.source, d.revision as number | null));
  const seen: { key: string; start: number; end: number }[] = [];
  for (const span of [...sources, ...gaps]) {
    const key = physicalSourceKey(span.source);
    if (seen.some((prior) => prior.key === key && prior.start < span.end && prior.end > span.start)) throw new LogContractError("summary request contains overlapping source ranges");
    seen.push({key, start:span.start, end:span.end});
  }
  if (d.priorCumulativeEntryId !== null) id(d.priorCumulativeEntryId, "priorCumulativeEntryId");
  parseHealthSnapshot(d.dbHealth); timestamp(d.requestedAt, "requestedAt");
  if (!sources.length && !gaps.length) throw new LogContractError("summary request needs new source or gap");
  if (d.inputKey !== summaryInputKey(subject, d.revision as number | null, sources, gaps, d.skillDigest as string)) throw new LogContractError("inputKey mismatch");
  if (Buffer.byteLength(canonicalJson(d), "utf8") > MAX_BODY_BYTES) throw new LogContractError("summary request exceeds 1MiB");
  return d as unknown as SummaryRequest;
}
export function parseSummaryResult(value: unknown): SummaryResult {
  const d = record(value, "SummaryResult", ["schemaVersion", "requestId", "inputKey", "skillDigest", "sources", "facts", "interpretations", "unknowns", "usage"]);
  id(d.requestId, "requestId"); sha(d.inputKey, "inputKey"); sha(d.skillDigest, "skillDigest");
  array(d.sources, "sources", 128).forEach(parseSourceRange);
  for (const key of ["facts", "interpretations", "unknowns"]) strings(d[key], key);
  const usage = record(d.usage, "SummaryUsage", ["schemaVersion", "inputTokens", "outputTokens", "costUsd"]);
  for (const key of ["inputTokens", "outputTokens"]) if (usage[key] !== null) integer(usage[key], key);
  if (usage.costUsd !== null && (typeof usage.costUsd !== "string" || usage.costUsd.length > 32 || !/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?$/.test(usage.costUsd))) throw new LogContractError("costUsd invalid");
  if (Buffer.byteLength(canonicalJson(d), "utf8") > MAX_SUMMARY_RESULT_BYTES) throw new LogContractError("summary result exceeds 8KiB");
  return d as unknown as SummaryResult;
}

export function serializeLogContract(value: SubjectRef | SourceRef | SourceRange | ObservationBoundary | ObservationCoverage | RawReadResult | ImportantEntry | HealthSnapshot | SummaryRequest | SummaryResult): string {
  return canonicalJson(value);
}
