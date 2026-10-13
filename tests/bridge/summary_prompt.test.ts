/** Shared pure prompt construction preserves the pre-extraction wire bytes. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { canonicalJson } from "../../omp_bridge/contract/logs_v1.ts";
import { buildSummaryPrompt } from "../../omp_bridge/g3/summary_sdk.ts";

test("shared builder preserves exact prior string, field order and UTF-8 bytes", () => {
  const input = { request: { z: "한글", a: 1 }, selected: [{ text: "work", range: { end: 4, start: 0 } }],
    omitted: [], priorCumulative: null, metadata: { ignored: true } };
  const before = JSON.stringify(input);
  const actual = buildSummaryPrompt(input);
  const expected = '{"request":{"z":"한글","a":1},"selected":[{"text":"work","range":{"end":4,"start":0}}],"omitted":[],"priorCumulative":null,"resultByteLimit":8192,"instruction":"Use db_summary_save exactly once, then stop."}';
  assert.equal(actual, expected);
  assert.deepEqual(Buffer.from(actual, "utf8"), Buffer.from(expected, "utf8"));
  assert.equal(JSON.stringify(input), before);
});

test("verifier canonical snapshot and dispatch detached snapshot have identical prompt bytes", () => {
  const prepared = { metadata: { private: true }, definitionDigest: "definition",
    request: { requestedAt: "2026-10-13T00:00:00Z", requestId: "request", subject: { kind: "command", id: "subject" } },
    selected: [{ text: "값", range: { start: 0, end: 3 } }], omitted: [], priorCumulative: null };
  const dispatchInput = JSON.parse(canonicalJson({ request: prepared.request, selected: prepared.selected,
    omitted: prepared.omitted, priorCumulative: prepared.priorCumulative,
    definitionDigest: prepared.definitionDigest, metadata: prepared.metadata }));
  const verifierInput = JSON.parse(canonicalJson(prepared));
  const priorPrompt = JSON.stringify({ request: dispatchInput.request, selected: dispatchInput.selected,
    omitted: dispatchInput.omitted, priorCumulative: dispatchInput.priorCumulative, resultByteLimit: 8192,
    instruction: "Use db_summary_save exactly once, then stop." });
  assert.equal(buildSummaryPrompt(dispatchInput), priorPrompt);
  assert.equal(buildSummaryPrompt(verifierInput), priorPrompt);
});
