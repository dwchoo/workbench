import copy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import unittest
from uuid import uuid4

from workbench.contracts.logs_v1 import (
    LogContractError, canonical_json, decode_json, parse_health_snapshot, parse_important_entry,
    parse_observation_boundary, parse_observation_coverage,
    parse_raw_read_result, parse_source_range, parse_source_ref, parse_subject_ref,
    parse_summary_request, parse_summary_result, summary_input_key,
)


def fixture():
    task = str(uuid4())
    run = str(uuid4())
    subject = {"schemaVersion": 1, "kind": "task", "id": task}
    source = {"schemaVersion": 1, "sourceKind": "terminal", "collectorEpoch": 1,
              "taskId": task, "revision": 1, "runId": run}
    raw = b"abc"
    span = {"schemaVersion": 1, "source": source, "start": 0, "end": len(raw),
            "digest": sha256(raw).hexdigest()}
    body = {"schemaVersion": 1, "facts": ["사실"], "interpretations": [], "unknowns": ["unknown"]}
    body_bytes = canonical_json(body)
    entry = {"schemaVersion": 1, "entryId": str(uuid4()), "subject": subject,
             "kind": "start", "sources": [span], "sourceGaps": [], "recordedAt": "2026-10-09T01:02:03Z",
             "body": body, "bodyCodec": "identity-v1", "decodedBytes": len(body_bytes),
             "storedBytes": len(body_bytes), "bodySha256": sha256(body_bytes).hexdigest(),
             "originDbUuid": str(uuid4())}
    health = {"schemaVersion": 1, "dbUuid": str(uuid4()), "measuredAt": "2026-10-09T01:02:03Z",
              "stale": False, "mainBytes": 1, "walBytes": 2, "shmBytes": 3, "totalBytes": 6,
              "logicalLiveBytes": 0, "freelistBytes": 0, "subjectStats": [],
              "cleanupCandidates": None, "lastWrite": None, "lastLock": None,
              "lastMaintenance": None, "diskFreeBytes": None,
              "lastQuickCheck": None, "metadataOutsideBudget": True}
    skill_digest = sha256(b"skill").hexdigest()
    request = {"schemaVersion": 1, "requestId": str(uuid4()), "subject": subject,
               "revision": 1, "authorizationRef": str(uuid4()), "authorizationEpoch": 1,
               "inputKey": summary_input_key(subject, 1, [span], [], skill_digest),
               "skillName": "workbench-important-log", "skillDigest": skill_digest,
               "sources": [span], "sourceGaps": [], "priorCumulativeEntryId": None,
               "dbHealth": health, "requestedAt": "2026-10-09T01:02:03Z"}
    result = {"schemaVersion": 1, "requestId": request["requestId"],
              "inputKey": request["inputKey"], "skillDigest": skill_digest,
              "sources": [span], "facts": ["fact"], "interpretations": [], "unknowns": [],
              "usage": {"schemaVersion": 1, "inputTokens": None,
                        "outputTokens": None, "costUsd": None}}
    raw_result = {"schemaVersion": 1, "source": source, "status": "available",
                  "chunks": [{"schemaVersion": 1, "range": span, "dataBase64": "YWJj"}],
                  "available": [span], "missing": []}
    return subject, span, raw_result, entry, health, request, result


class LogsV1ContractTests(unittest.TestCase):
    def test_source_kind_physical_ids_and_structured_offsets_match_typescript(self):
        subject, span, _, _, _, request, _ = fixture()
        terminal = span["source"]
        message_a = {**terminal, "sourceKind": "manager_instruction", "messageId": str(uuid4())}
        message_b = {**message_a, "messageId": str(uuid4())}
        report_event = {**terminal, "sourceKind": "report", "eventId": str(uuid4())}
        good = [terminal, message_a, message_b, report_event]
        bad = [
            {**terminal, "messageId": str(uuid4())},
            {**terminal, "eventId": str(uuid4())},
            {**terminal, "commandId": str(uuid4())},
            {**message_a, "eventId": str(uuid4())},
            {**message_a, "messageId": None},
            {**terminal, "sourceKind": "manager_instruction"},
        ]
        self.assertEqual([parse_source_ref(value) for value in good], good)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(LogContractError):
                parse_source_ref(value)
        spans = [{**span, "source": source} for source in (message_a, message_b, report_event)]
        structured = {**request, "sources": spans}
        structured["inputKey"] = summary_input_key(subject, 1, spans, [], request["skillDigest"])
        self.assertEqual(parse_summary_request(structured), structured)
        overlap = copy.deepcopy(structured)
        overlap["sources"][1]["source"]["messageId"] = message_a["messageId"]
        overlap["inputKey"] = summary_input_key(subject, 1, overlap["sources"], [], request["skillDigest"])
        with self.assertRaises(LogContractError):
            parse_summary_request(overlap)
        program = """import fs from 'node:fs'; import * as c from './omp_bridge/contract/logs_v1.ts';
const x=JSON.parse(fs.readFileSync(0,'utf8'));
const valid=x.good.map(v=>c.parseSourceRef(v));
const invalid=x.bad.map(v=>{try{c.parseSourceRef(v);return false}catch(e){return e.code==='invalid_log_contract_v1'}});
const accepted=c.parseSummaryRequest(x.structured);
let overlapRejected=false;
try{c.parseSummaryRequest(x.overlap)}catch(e){overlapRejected=e.code==='invalid_log_contract_v1'}
process.stdout.write(JSON.stringify({valid,invalid,accepted,overlapRejected}));"""
        result = subprocess.run(
            ["node", "--experimental-strip-types", "--input-type=module", "-e", program],
            input=json.dumps({"good": good, "bad": bad, "structured": structured, "overlap": overlap}),
            text=True, capture_output=True, cwd=Path(__file__).resolve().parents[2], check=True)
        self.assertEqual(json.loads(result.stdout), {
            "valid": good, "invalid": [True] * len(bad),
            "accepted": structured, "overlapRejected": True,
        })

    def test_golden_summary_request_result_bytes_and_node_execution(self):
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "logs-summary-v1.json"
        golden = json.loads(fixture_path.read_text(encoding="utf-8"))
        request = parse_summary_request(golden["request"])
        result = parse_summary_result(golden["result"])
        for name, value in (("request", request), ("result", result)):
            self.assertEqual(sha256(canonical_json(value)).hexdigest(), golden["canonicalSha256"][name])
        program = """import fs from 'node:fs'; import {createHash} from 'node:crypto';
import * as c from './omp_bridge/contract/logs_v1.ts';
const golden = JSON.parse(fs.readFileSync(0,'utf8'));
const values = {request:c.parseSummaryRequest(golden.request),result:c.parseSummaryResult(golden.result)};
const hashes = Object.fromEntries(Object.entries(values).map(([key,value]) =>
  [key,createHash('sha256').update(c.canonicalJson(value),'utf8').digest('hex')]));
process.stdout.write(JSON.stringify(hashes));"""
        ts = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", program],
                            input=json.dumps(golden, ensure_ascii=False), text=True, capture_output=True,
                            cwd=Path(__file__).resolve().parents[2], check=True)
        self.assertEqual(json.loads(ts.stdout), golden["canonicalSha256"])

    def test_python_types_and_shared_typescript_fixture(self):
        values = fixture()
        parsers = (parse_subject_ref, parse_source_range, parse_raw_read_result,
                   parse_important_entry, parse_health_snapshot, parse_summary_request,
                   parse_summary_result)
        self.assertEqual([parser(value) for parser, value in zip(parsers, values)], list(values))
        program = """import fs from 'node:fs';
import * as c from './omp_bridge/contract/logs_v1.ts';
const names = ['parseSubjectRef','parseSourceRange','parseRawReadResult','parseImportantEntry','parseHealthSnapshot','parseSummaryRequest','parseSummaryResult'];
const values = JSON.parse(fs.readFileSync(0,'utf8'));
const parsed = values.map((value,i) => c[names[i]](value));
process.stdout.write(JSON.stringify({parsed, inputKey:c.summaryInputKey(values[0],1,[values[1]],[],values[5].skillDigest)}));"""
        result = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", program],
                                input=json.dumps(values, ensure_ascii=False), text=True, capture_output=True,
                                cwd=Path(__file__).resolve().parents[2], check=True)
        ts = json.loads(result.stdout)
        self.assertEqual(ts["parsed"], list(values))
        self.assertEqual(ts["inputKey"], values[5]["inputKey"])

    def test_strict_negative_contract(self):
        subject, span, raw, entry, health, request, result = fixture()
        failures = []
        for index, key, value in ((0, "id", "../bad"), (1, "end", 0),
                                  (2, "status", "complete"), (3, "bodySha256", "0" * 64),
                                  (4, "totalBytes", 7), (5, "inputKey", "0" * 64),
                                  (6, "schemaVersion", 2)):
            candidate = copy.deepcopy((subject, span, raw, entry, health, request, result)[index])
            candidate[key] = value
            failures.append((index, candidate))
        foreign_entry = copy.deepcopy(entry)
        foreign_entry["sources"][0]["source"]["taskId"] = str(uuid4())
        failures.append((3, foreign_entry))
        wrong_revision = copy.deepcopy(request)
        wrong_revision["sources"][0]["source"]["revision"] = 2
        failures.append((5, wrong_revision))
        duplicate_range = copy.deepcopy(request)
        duplicate_range["sources"].append(copy.deepcopy(duplicate_range["sources"][0]))
        duplicate_range["inputKey"] = summary_input_key(duplicate_range["subject"], 1,
                                                        duplicate_range["sources"], [], duplicate_range["skillDigest"])
        failures.append((5, duplicate_range))
        for index, candidate in failures:
            with self.subTest(index=index), self.assertRaises(LogContractError):
                (parse_subject_ref, parse_source_range, parse_raw_read_result,
                 parse_important_entry, parse_health_snapshot, parse_summary_request,
                 parse_summary_result)[index](candidate)
        with self.assertRaises(LogContractError):
            parse_summary_result({**result, "facts": ["x" * 8192]})
        with self.assertRaises(LogContractError):
            parse_subject_ref({**subject, "unexpected": True})
        program = """import fs from 'node:fs'; import * as c from './omp_bridge/contract/logs_v1.ts';
const names = ['parseSubjectRef','parseSourceRange','parseRawReadResult','parseImportantEntry','parseHealthSnapshot','parseSummaryRequest','parseSummaryResult'];
const cases = JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(cases.map(([index,value]) => { try { c[names[index]](value); return true; } catch (e) { return false; } })));"""
        ts = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", program],
                            input=json.dumps(failures, ensure_ascii=False), text=True,
                            capture_output=True, cwd=Path(__file__).resolve().parents[2], check=True)
        self.assertEqual(json.loads(ts.stdout), [False] * len(failures))

    def test_json_duplicate_fields_are_rejected_in_both_languages(self):
        from workbench.contracts.logs_v1 import decode_json
        program = """import {decodeJson} from './omp_bridge/contract/logs_v1.ts';
try { decodeJson(process.argv[1]); process.stdout.write('accepted'); } catch (e) { process.stdout.write('rejected'); }"""
        for payload in ('{"schemaVersion":1,"kind":"task","kind":"command","id":"00000000-0000-0000-0000-000000000001"}',
                        '{"schemaVersion":1.0,"kind":"task","id":"00000000-0000-0000-0000-000000000001"}'):
            with self.subTest(payload=payload), self.assertRaises(LogContractError):
                decode_json(payload)
            ts = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", program, payload],
                                text=True, capture_output=True, cwd=Path(__file__).resolve().parents[2], check=True)
            self.assertEqual(ts.stdout, "rejected")

    def test_cost_uses_decimal_string_for_shared_bytes(self):
        result = fixture()[-1]
        result["usage"]["costUsd"] = "0.012345"
        self.assertEqual(parse_summary_result(result), result)
        bad = copy.deepcopy(result)
        bad["usage"]["costUsd"] = 0.012345
        with self.assertRaises(LogContractError):
            parse_summary_result(bad)
        program = """import fs from 'node:fs'; import {parseSummaryResult,canonicalJson} from './omp_bridge/contract/logs_v1.ts';
const value = JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(canonicalJson(parseSummaryResult(value)));"""
        ts = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", program],
                            input=json.dumps(result, ensure_ascii=False), text=True,
                            capture_output=True, cwd=Path(__file__).resolve().parents[2], check=True)
        self.assertEqual(ts.stdout.encode("utf-8"), canonical_json(result))

    def test_observation_witness_empty_and_strict_unicode_time_parity(self):
        subject, span, _, entry, _, request, _ = fixture()
        source = span["source"]
        start = {"schemaVersion": 1, "eventId": str(uuid4()), "source": source,
                 "kind": "start", "offset": 0, "observedAt": "2026-10-09T01:02:03Z"}
        final = {**start, "eventId": str(uuid4()), "kind": "final_drain"}
        witness = {"schemaVersion": 1, "coverageId": str(uuid4()), "source": source,
                   "startEventId": start["eventId"], "finalEventId": final["eventId"],
                   "finalOffset": 0, "sourceIds": []}
        self.assertEqual(parse_observation_boundary(start), start)
        self.assertEqual(parse_observation_boundary(final), final)
        self.assertEqual(parse_observation_coverage(witness), witness)
        for bad in ("\ud800", "\udc00"):
            with self.assertRaises(LogContractError):
                canonical_json({"x": bad})
            with self.assertRaises(LogContractError):
                canonical_json({bad: "x"})
            with self.assertRaises(LogContractError):
                decode_json('{"x":"\\ud800"}')
            with self.assertRaises(LogContractError):
                parse_important_entry({**entry, "recordedAt": "0000-01-01T00:00:00Z"})
        overlap = copy.deepcopy(request)
        overlap["sourceGaps"] = [{"schemaVersion": 1, "source": source, "start": 1,
                                  "end": 2, "reason": "unknown"}]
        overlap["inputKey"] = summary_input_key(subject, 1, overlap["sources"], overlap["sourceGaps"], overlap["skillDigest"])
        with self.assertRaises(LogContractError):
            parse_summary_request(overlap)
        program = """import * as c from './omp_bridge/contract/logs_v1.ts';
const source = JSON.parse(process.argv[1]); const start = JSON.parse(process.argv[2]);
const final = JSON.parse(process.argv[3]); const witness = JSON.parse(process.argv[4]);
const checks = [c.parseObservationBoundary(start), c.parseObservationBoundary(final), c.parseObservationCoverage(witness)];
const invalid = [() => c.canonicalJson({x:'\\ud800'}), () => c.canonicalJson({'\\udc00':'x'}),
  () => c.decodeJson('{"x":"\\\\ud800"}'), () => c.parseObservationBoundary({...start,observedAt:'0000-01-01T00:00:00Z'})];
process.stdout.write(JSON.stringify({checks, rejected: invalid.map(fn => {try {fn(); return false} catch(e) {return e.code==='invalid_log_contract_v1'}})}));"""
        ts = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", program,
                             json.dumps(source), json.dumps(start), json.dumps(final), json.dumps(witness)],
                            text=True, capture_output=True, cwd=Path(__file__).resolve().parents[2], check=True)
        self.assertEqual(json.loads(ts.stdout), {"checks": [start, final, witness], "rejected": [True] * 4})
