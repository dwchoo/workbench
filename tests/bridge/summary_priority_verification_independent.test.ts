/** Actual native-entry JSON view helper; no SDK loader/native capability claim. */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../omp_bridge/g3/summary_priority_verification.ts", import.meta.url), "utf8");
const start = source.indexOf("export function observeNativeEntries(");
const end = source.indexOf("\nconst fields =", start);
assert.ok(start >= 0 && end > start, "actual JSON view helper unavailable");
const helper = source.slice(start, end).replace("export function", "function");
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
const observe = vm.runInNewContext(stripTypeScriptTypes(helper + "\nobserveNativeEntries;"), { sha, Buffer });

test("entry facts and marker use the same normal JSON.stringify view without dropping entries", () => {
  const entries = [
    { type: "session", id: "first", parentId: undefined, optional: undefined,
      data: [undefined, 1.25], at: new Date("2026-10-14T00:00:00Z") },
    { type: "message", id: "second", parentId: "first", content: "private-work-owned-marker" },
    { type: "custom", id: "third", parentId: "second", optional: undefined, decimal: 0.125 }
  ];
  const before = entries.map(entry => JSON.stringify(entry));
  const observed = JSON.parse(JSON.stringify(observe(entries)));
  assert.equal(observed.entries, entries.length);
  assert.equal(observed.allEntries.length, entries.length);
  assert.equal(observed.rawUpperMarkerSeen, true);
  for (let index = 0; index < entries.length; index++) {
    const view = JSON.parse(before[index]), facts = observed.allEntries[index];
    assert.equal(facts.type, view.type ?? null); assert.equal(facts.id, view.id ?? null);
    assert.equal(facts.parentId, view.parentId ?? null);
    assert.equal(facts.bytes, Buffer.byteLength(before[index]));
    assert.equal(facts.native_JSON_serialized_entry_sha256, sha(before[index]));
  }
  assert.deepEqual(entries.map(entry => JSON.stringify(entry)), before);
  assert.match(observed.entryView, /native JSON.stringify/);
  assert.match(observed.entryView, /storage serialization equality unknown/);
});

test("serialization failure refuses the whole observation instead of silently dropping an entry", () => {
  const circular: any = { type: "message", id: "cannot-serialize", parentId: null };
  circular.content = circular;
  assert.throws(() => observe([{ type: "session", id: "first" }, circular]), /circular/i);
  assert.throws(() => observe([undefined]), /native_entry_JSON_string_unavailable/);
});
