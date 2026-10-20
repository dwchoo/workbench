import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { parseRequest, parseRecord } from "../../omp_bridge/contract/records_v1.ts";

const fixture = JSON.parse(readFileSync(new URL("../contracts/fixtures/work-records-v1.json", import.meta.url), "utf8"));
test("handwritten requests and record snapshot roundtrip", () => {
    assert.deepEqual(fixture.requests.map(parseRequest), fixture.requests);
    assert.deepEqual(parseRecord(fixture.record), fixture.record);
});
test("stored snapshots reject invalid identity revision role and time", () => {
    for (const changed of [{revision:true}, {actor:"model-supplied"}, {record_id:"bad"},
        {recorded_at:"not-a-time"}, {created_at:"2026-02-30T00:00:00Z"}, {body:"\ud800"}, {extra:1}]) {
        assert.throws(() => parseRecord({...fixture.record, ...changed}));
    }
});
