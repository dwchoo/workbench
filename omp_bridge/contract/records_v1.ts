/** Version 1 explicit-record wire contract. Python owns persistence and export. */
export const SCHEMA_VERSION = 1;
export const MAX_TITLE_CHARS = 256;
export const MAX_BODY_BYTES = 64 * 1024;
export const MAX_QUERY_CHARS = 256;
export const DEFAULT_LIMIT = 20;
export const MAX_LIMIT = 100;
export const RECENT_LIMIT = 5;
export const PREVIEW_CHARS = 160;
export const MAX_CONTEXT_BYTES = 16 * 1024;
export const MAX_RESPONSE_BYTES = 900_000;
export class RecordContractError extends Error { code = "invalid_record_contract_v1"; }
export const REQUEST_FIELDS = ["action", "record_id", "revision", "expected_revision", "title", "body", "task_id", "query", "limit", "after", "output_path"];
const actionFields: Record<string, [string[], string[]]> = {
    create: [["title", "body"], ["task_id"]],
    revise: [["record_id", "expected_revision", "title", "body"], ["task_id"]],
    show: [["record_id"], ["revision"]], history: [["record_id"], ["limit", "after"]],
    list: [[], ["task_id", "limit", "after"]], search: [["query"], ["task_id", "limit", "after"]],
    recent: [[], []], export: [["record_id"], ["revision", "output_path"]],
};
function text(value: unknown, name: string): asserts value is string {
    if (typeof value !== "string" || !value || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value))
        throw new RecordContractError(`${name} must be nonempty UTF-8`);
}
function positive(value: unknown): void {
    if (!Number.isSafeInteger(value) || (value as number) < 1) throw new RecordContractError("positive safe integer required");
}
function recordId(value: unknown): void {
    if (typeof value !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)) throw new RecordContractError("canonical record UUID required");
}
export function parseRequest(input: unknown): Record<string, any> {
    if (!input || typeof input !== "object" || Array.isArray(input)) throw new RecordContractError("object required");
    const value = input as Record<string, any>;
    if (Object.keys(value).length !== REQUEST_FIELDS.length || !REQUEST_FIELDS.every(k => Object.hasOwn(value, k))) throw new RecordContractError("unsupported fields");
    if (typeof value.action !== "string" || !Object.hasOwn(actionFields, value.action)) throw new RecordContractError("unsupported action");
    const [required, optional] = actionFields[value.action];
    for (const key of REQUEST_FIELDS.filter(k => k !== "action")) {
        const item = value[key];
        if (required.includes(key) && item === null) throw new RecordContractError(`${key} required`);
        if (![...required, ...optional].includes(key) && item !== null) throw new RecordContractError(`${key} unused`);
        if (item === null) continue;
        if (key === "record_id") recordId(item);
        else if (["revision", "expected_revision", "limit"].includes(key)) {
            positive(item);
            if (key === "limit" && item > MAX_LIMIT) throw new RecordContractError("limit_too_large");
        } else {
            text(item, key);
            if (key === "title" && [...item].length > MAX_TITLE_CHARS) throw new RecordContractError("title_too_large");
            if (key === "body" && Buffer.byteLength(item, "utf8") > MAX_BODY_BYTES) throw new RecordContractError("body_too_large");
            if (key === "query" && [...item].length > MAX_QUERY_CHARS) throw new RecordContractError("query_too_large");
            if (key === "task_id" && item.trim() !== item) throw new RecordContractError("invalid task_id");
            if (key === "output_path" && item.includes("\0")) throw new RecordContractError("invalid output_path");
        }
    }
    return structuredClone(value);
}
export function parseRecord(input: unknown): Record<string, any> {
    if (!input || typeof input !== "object" || Array.isArray(input)) throw new RecordContractError("snapshot required");
    const value = input as Record<string, any>;
    const keys = ["record_id", "revision", "title", "body", "task_id", "created_at", "recorded_at", "actor"];
    if (Object.keys(value).length !== keys.length || !keys.every(k => Object.hasOwn(value, k))) throw new RecordContractError("unsupported snapshot fields");
    recordId(value.record_id); positive(value.revision);
    text(value.title, "title"); text(value.body, "body");
    if ([...value.title].length > MAX_TITLE_CHARS || Buffer.byteLength(value.body, "utf8") > MAX_BODY_BYTES) throw new RecordContractError("snapshot too large");
    if (value.task_id !== null) { text(value.task_id, "task_id"); if (value.task_id.trim() !== value.task_id) throw new RecordContractError("invalid task_id"); }
    if (!["manager", "worker"].includes(value.actor)) throw new RecordContractError("invalid actor");
    for (const key of ["created_at", "recorded_at"]) {
        const item = value[key];
        if (typeof item !== "string" || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$/.test(item) || item.startsWith("0000")) throw new RecordContractError("UTC timestamp required");
        const date = new Date(item);
        if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 19) !== item.slice(0, 19)) throw new RecordContractError("invalid UTC timestamp");
    }
    return structuredClone(value);
}
