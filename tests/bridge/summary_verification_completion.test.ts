/** Actual SDK-free publication helper and private filesystem observations. */
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { publishVerificationCompletion } from "../../omp_bridge/g3/verification_completion.ts";

function ownedRoot(t: any): string {
  const root = mkdtempSync(join(tmpdir(), "workbench-completion-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return root;
}

for (const kind of ["ok", "err"] as const) {
  test(`${kind} serializes before creating files and publishes private complete JSON`, t => {
    const root = ownedRoot(t);
    const expected = { kind, facts: ["한글 complete JSON", "x".repeat(32768)] };
    let serialized = false;
    publishVerificationCompletion(root, { toJSON() {
      assert.deepEqual(readdirSync(root), []);
      serialized = true;
      return expected;
    } }, kind);
    assert.equal(serialized, true);
    assert.deepEqual(JSON.parse(readFileSync(join(root, "completed.json"), "utf8")), expected);
    assert.equal(statSync(join(root, "completed.json")).mode & 0o777, 0o600);
    assert.deepEqual(readdirSync(root), ["completed.json"]);
  });
}

test("stale success temporary file cannot block error publication", t => {
  const root = ownedRoot(t);
  const stale = join(root, ".completed.ok.tmp");
  writeFileSync(stale, "incomplete success bytes", { mode: 0o600 });
  const error = { error: "success publication failed", exercised: [] };
  publishVerificationCompletion(root, error, "err");
  assert.deepEqual(JSON.parse(readFileSync(join(root, "completed.json"), "utf8")), error);
  assert.equal(statSync(join(root, "completed.json")).mode & 0o777, 0o600);
  assert.equal(readFileSync(stale, "utf8"), "incomplete success bytes");
  assert.equal(existsSync(join(root, ".completed.err.tmp")), false);
});

test("serialization failure leaves completion and temporary files absent", t => {
  const root = ownedRoot(t);
  const cycle: any = {};
  cycle.self = cycle;
  assert.throws(() => publishVerificationCompletion(root, cycle, "ok"));
  assert.deepEqual(readdirSync(root), []);
});
