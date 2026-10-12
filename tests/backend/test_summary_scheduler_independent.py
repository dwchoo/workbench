"""Independent DVL-10 requirements tests; real stores and private producers.

The public requirement boundary is copied source ownership and capture failure
isolation. Controlled stdout drains are not an SDK/model runtime claim.
"""
from hashlib import sha256
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from workbench.backend.flow_terminal import TerminalService, _Command
from workbench.backend.paths import DataLayout, ensure_private_dir
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogCatalog, ImportantLogStore
from workbench.storage.log_raw.port import task_source
from workbench.tasks.repository import TaskRepository
from workbench.workflow.run import WorkflowRun


AT = "2026-10-12T00:00:00Z"
EVIDENCE = Path(os.environ.get("DVL10_INDEPENDENT_OUTPUT", ".workflow/dev-validation-log-retention/runs/dvl10-20261012/test02"))


class ProducerCaptureBoundaryTests(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="producer-", dir=EVIDENCE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.layout = DataLayout(self.root)
        ensure_private_dir(self.layout.workflow / "terminal")
        self.catalog = ImportantLogCatalog(self.layout.important_logs, create=True)
        self.sink = ActiveSubjectLog(self.catalog)
        self.factory = lambda: TaskRepository(self.layout.tasks)
        self.failures = []

    def task_run(self):
        journal = self.factory()
        self.addCleanup(journal.close)
        task = journal.create_task({"goal": "independent observed work"})
        journal.approve_scope(task, 1, {"paths": ["fixture"]})
        journal.proceed(task, 1, "approved private fixture")
        run = journal.start_run(task, 1)
        self.sink.record_flow({"seq": 1, "at": AT, "type": "delegated", "task_id": task,
            "revision": 1, "scope_decision_id": str(uuid4()), "proceed_decision_id": str(uuid4())})
        owner = WorkflowRun.__new__(WorkflowRun)
        owner.repository, owner.task_id, owner.revision, owner.run_id = journal, task, 1, run
        owner._source_ref = task_source(task, 1, run)
        owner._source_ids, owner._evidence_error = [], None
        owner._record = {}
        owner.subject_log_source, owner.subject_log = lambda: self.sink, None
        owner.subject_capture_expected, owner.subject_capture_unknown = True, False
        owner.subject_failure = lambda: self.failures.append("capture_unknown")
        return owner, {"schemaVersion": 1, "kind": "task", "id": task}

    def entries(self, subject):
        with self.catalog._lease() as fd:
            active = self.catalog._current(fd)["active"]
            with ImportantLogStore.open(self.catalog.directory, active, readonly=True) as store:
                return [store.get_entry(row[0]) for row in store._connection.execute(
                    "SELECT entry_id FROM entries WHERE subject_kind=? AND subject_id=?",
                    (subject["kind"], subject["id"]))]

    def test_workflow_101_independent_drains_are_copied_without_first100_cutoff(self):
        owner, subject = self.task_run()
        payloads = [f"{number:03d}\n".encode() for number in range(101)]
        # Exercise separate producer drain invocations, rather than a metadata scan.
        for number, data in enumerate(payloads):
            owner._record_observed([(4 * number, data, False)])
        spans = [span for entry in self.entries(subject) for span in entry["sources"]
                 if span["source"]["sourceKind"] == "terminal"]
        self.assertEqual(len(spans), 101)
        self.assertEqual({(s["start"], s["end"], s["digest"]) for s in spans},
                         {(i * 4, (i + 1) * 4, sha256(data).hexdigest()) for i, data in enumerate(payloads)})
        self.assertEqual({s["source"]["runId"] for s in spans}, {owner.run_id})
        self.assertEqual({s["source"]["revision"] for s in spans}, {1})
        self.assertFalse(owner.subject_capture_unknown)
        self.assertEqual(self.failures, [])

    def test_workflow_failed_observations_keep_exact_core_spans_and_sticky_unknown(self):
        # WorkflowRun owns independently confirmed spans: each observation is
        # attempted, unlike Terminal's continuity-dependent first-failure latch.
        owner, subject = self.task_run()
        attempts = []
        class FailedSink:
            def record_terminal_range(self, *args, **kwargs):
                attempts.append({"start": args[2]["start"], "end": args[2]["end"]})
                raise OSError("owned independent important sink unavailable")
        owner.subject_log_source = lambda: FailedSink()
        owner._record_observed([(0, b"one\n", False)])
        owner._record_observed([(4, b"two\n", False)])
        owner._record_observed([(8, b"tri\n", False)])
        evidence = {"sink_attempts": attempts, "failure_notifications": list(self.failures),
                    "capture_unknown": owner.subject_capture_unknown,
                    "core_metadata_sources": len(owner.repository.get_log_sources(subject)),
                    "important_terminal_ranges": sum(len(entry["sources"]) for entry in self.entries(subject)
                                                      if any(s["source"]["sourceKind"] == "terminal" for s in entry["sources"]))}
        (EVIDENCE / "workflow-per-observation-failure.json").write_text(json.dumps(evidence, indent=2))
        self.assertTrue(owner.subject_capture_unknown)
        self.assertEqual(attempts, [{"start": 0, "end": 4}, {"start": 4, "end": 8}, {"start": 8, "end": 12}])
        self.assertEqual(self.failures, ["capture_unknown"] * 3)
        self.assertEqual(evidence["core_metadata_sources"], 3)
        self.assertEqual(evidence["important_terminal_ranges"], 0)
        self.assertIsNone(owner._evidence_error)

    def test_workflow_middle_failure_preserves_first_and_third_important_spans(self):
        owner, subject = self.task_run()
        attempts = []
        actual = self.sink
        class MiddleFailure:
            def record_terminal_range(self, *args, **kwargs):
                attempts.append(args[2]["start"])
                if args[2]["start"] == 4:
                    raise OSError("owned middle observation failure")
                return actual.record_terminal_range(*args, **kwargs)
        owner.subject_log_source = lambda: MiddleFailure()
        for start, data in ((0, b"one\n"), (4, b"two\n"), (8, b"tri\n")):
            owner._record_observed([(start, data, False)])
        spans = [span for entry in self.entries(subject) for span in entry["sources"]
                 if span["source"]["sourceKind"] == "terminal"]
        self.assertEqual(attempts, [0, 4, 8])
        self.assertEqual({(s["start"], s["end"]) for s in spans}, {(0, 4), (8, 12)})
        self.assertEqual(len(owner.repository.get_log_sources(subject)), 3)
        self.assertEqual(self.failures, ["capture_unknown"])
        self.assertTrue(owner.subject_capture_unknown)
        self.assertIsNone(owner._evidence_error)

    def test_terminal_failure_latches_without_changing_raw_tail_or_core_result(self):
        identity = str(uuid4())
        path = self.layout.workflow / "terminal" / (identity + ".log")
        path.touch(mode=0o600)
        command = _Command(identity, "private fixture", {}, None, path)
        attempts = []
        def fail(start, data):
            attempts.append((start, data))
            raise OSError("owned fixture sink unavailable")
        command.summary_started = True
        command.summary_capture, command.summary_failure = fail, lambda: self.failures.append("capture_unknown")
        core_result = {"status": "exited", "exit_code": 0}
        command.result = core_result
        command.notice = "pending"
        command.write(b"one\n")
        command.write(b"two\n")
        command.write(b"tri\n")
        self.assertEqual(attempts, [(0, b"one\n")])
        self.assertEqual(self.failures, ["capture_unknown"])
        self.assertEqual(path.read_bytes(), b"one\ntwo\ntri\n")
        self.assertEqual(command.tail()["output_tail"], "one\ntwo\ntri\n")
        self.assertIs(command.result, core_result)
        self.assertEqual(command.notice, "pending")

    def observed_request(self, subject, spans):
        decision=self.sink.authority(subject,active=True,paused=False,revoked=False,has_new_input=True)
        self.assertTrue(decision["allowed"])
        request={"schemaVersion":1,"requestId":str(uuid4()),"subject":subject,"revision":decision["revision"],
            "authorizationRef":decision["authorization_ref"],"authorizationEpoch":decision["authorization_epoch"],
            "skillName":SKILL_NAME,"skillDigest":sha256(skill_bytes()).hexdigest(),"sources":deepcopy(spans),
            "sourceGaps":[],"priorCumulativeEntryId":None,"dbHealth":self.catalog.health_snapshot()["active"],"requestedAt":AT}
        request["inputKey"]=summary_input_key(subject,request["revision"],spans,[],request["skillDigest"])
        return request

    def observed_run_raw(self, owner, data):
        directory=self.layout.workflow/"runs"/owner.run_id
        ensure_private_dir(directory)
        path=directory/"raw.log";path.write_bytes(data);os.chmod(path,0o600)
        record=directory/"run.json"
        record.write_text(json.dumps({"run_id":owner.run_id,"raw_log":str(path)}));os.chmod(record,0o600)

    def confirmed_terminal_records(self, owner, subject):
        entries=[entry for entry in self.entries(subject) if any(span["source"]["sourceKind"]=="terminal" for span in entry["sources"])]
        for entry in entries:
            for span in entry["sources"]:owner.repository.ingest_summary_source(subject,span,entry["entryId"])
        return entries,owner.repository.pending_summary_sources(subject,revision=1)

    def test_worker_decision_response_uuid_reads_exact_observed_delivery_fact(self):
        owner,subject=self.task_run()
        response={"response_id":str(uuid4()),"decision":"작업 범위 안에서 계속 진행","revision":1}
        owner._copy_decision(response)
        self.assertFalse(owner.subject_capture_unknown)
        entry=next(entry for entry in self.entries(subject) if any(span["source"].get("eventId")==response["response_id"] for span in entry["sources"]))
        original=json.dumps(response,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
        span=entry["sources"][0]
        self.assertEqual(span["source"]["eventId"],response["response_id"])
        self.assertEqual((span["start"],span["end"],span["digest"]),(0,len(original),sha256(original).hexdigest()))
        owner.repository.ingest_summary_source(subject,span,entry["entryId"])
        records=owner.repository.pending_summary_sources(subject,revision=1)
        observation={"finding":"WORKER-DECISION-SOURCE-10","response":response,"original_text":original.decode(),"entry":entry,"records":records}
        try:
            pieces,gaps=SummarySourceCollector(self.root,lambda:self.sink).collect(owner.repository,subject,records)
        except SummaryDeferred as error:
            observation["observed_defer"]=str(error)
            (EVIDENCE/"review-decision.json").write_text(json.dumps(observation,ensure_ascii=False,indent=2))
            self.fail("confirmed Worker decision cannot be collected: "+str(error))
        observation["texts"]=[piece.text for piece in pieces]
        (EVIDENCE/"review-decision.json").write_text(json.dumps(observation,ensure_ascii=False,indent=2))
        self.assertEqual(gaps,[]);self.assertEqual([piece.range for piece in pieces],[span])
        self.assertEqual([piece.text for piece in pieces],[original.decode()])

    def test_producer_flow_grant_and_worker_decision_and_terminal_event_keep_exact_payloads(self):
        journal=self.factory();self.addCleanup(journal.close)
        task=journal.create_task({"goal":"실제 producer grant/Worker 자료 연결"})
        journal.approve_scope(task,1,{"paths":["fixture"]});journal.proceed(task,1,"approved private fixture")
        journal.start_run(task,1);subject={"schemaVersion":1,"kind":"task","id":task}
        flow=HandoffJournal(self.layout.workflow/"tasks-flow.jsonl");self.addCleanup(flow.close)
        grant=flow.append({"type":"delegated","task_id":task,"revision":1,
            "scope_decision_id":str(uuid4()),"proceed_decision_id":str(uuid4())})
        self.sink.record_flow(grant)
        owner=WorkflowRun.__new__(WorkflowRun);owner.task_id=task;owner.revision=1
        owner.subject_log_source=lambda:self.sink;owner.subject_log=None
        owner.subject_capture_expected=True;owner.subject_capture_unknown=False;owner.subject_failure=None
        response={"response_id":str(uuid4()),"decision":"현재 작업 범위에서 계속 검증","revision":1}
        owner._copy_decision(response);self.assertFalse(owner.subject_capture_unknown)
        terminal=HandoffJournal(self.layout.workflow/"handoffs.jsonl");self.addCleanup(terminal.close)
        started=terminal.append({"type":"terminal_started","command_id":str(uuid4()),"task_id":task,
            "command":"owned producer fixture","started_at":AT})
        self.sink.record_terminal(started,revision=1)
        entries=self.entries(subject)
        for entry in entries:
            for span in entry["sources"]:journal.ingest_summary_source(subject,span,entry["entryId"])
        records=journal.pending_summary_sources(subject,revision=1)
        pieces,gaps=SummarySourceCollector(self.root,lambda:self.sink).collect(journal,subject,records)
        originals=[json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode() for value in (grant,response,started)]
        self.assertEqual(gaps,[]);self.assertEqual(len(pieces),3)
        self.assertEqual(sorted(piece.text for piece in pieces),sorted(data.decode() for data in originals))
        spans=[piece.range for piece in pieces]
        self.assertEqual(sorted((s["end"]-s["start"],s["digest"]) for s in spans),sorted((len(data),sha256(data).hexdigest()) for data in originals))
        self.assertEqual(sum(s["source"]["sourceKind"]=="decision" for s in spans),2)
        self.assertEqual(sum(s["source"]["sourceKind"]=="event" for s in spans),1)
        request=self.observed_request(subject,spans)
        authorization=self.sink.authorize_summary_request(request,active=True,paused=False,revoked=False,has_new_input=True)
        self.assertTrue(authorization["allowed"])
        (EVIDENCE/"green-producer-decisions.json").write_text(json.dumps({"grant":grant,"worker":response,"terminal":started,
            "sources":spans,"texts":[p.text for p in pieces],"authorization":authorization},ensure_ascii=False,indent=2))

    def test_middle_important_copy_failure_keeps_only_confirmed_first_third_eligible(self):
        owner,subject=self.task_run();actual=self.sink
        class MiddleFailure:
            def record_terminal_range(self,*args,**kwargs):
                if args[2]["start"]==4:raise OSError("owned middle important copy failure")
                return actual.record_terminal_range(*args,**kwargs)
        owner.subject_log_source=lambda:MiddleFailure()
        for start,data in ((0,b"one\n"),(4,b"two\n"),(8,b"tri\n")):owner._record_observed([(start,data,False)])
        self.observed_run_raw(owner,b"one\ntwo\ntri\n")
        entries,pending=self.confirmed_terminal_records(owner,subject)
        expected=[span for entry in entries for span in entry["sources"]]
        confirmed=[row for row in pending if row["range"] in expected]
        pieces,gaps=SummarySourceCollector(self.root,lambda:self.sink).collect(owner.repository,subject,confirmed)
        request=self.observed_request(subject,[piece.range for piece in pieces])
        authorization=self.sink.authorize_summary_request(request,active=True,paused=False,revoked=False,has_new_input=True)
        observation={"finding":"UNCONFIRMED-CORE-SOURCE-10","core_records":owner.repository.get_log_sources(subject),
            "important_sources":expected,"actual_pending":pending,"remaining_texts":[p.text for p in pieces],
            "authorization":authorization,"capture_unknown":owner.subject_capture_unknown}
        (EVIDENCE/"review-confirmed-source.json").write_text(json.dumps(observation,ensure_ascii=False,indent=2))
        self.assertEqual(len(observation["core_records"]),3);self.assertEqual(len(expected),2)
        self.assertTrue(owner.subject_capture_unknown);self.assertEqual([p.text for p in pieces],["one\n","tri\n"])
        self.assertEqual(gaps,[]);self.assertTrue(authorization["allowed"])
        self.assertEqual([row["range"] for row in pending],expected,"uncopied middle core source must not enter summary eligible queue")

    def test_korean_matcher_split_preserves_exact_spans_and_complete_summary_text(self):
        from workbench.workflow.run import _ExactByteMatcher
        from workbench.backend.summary_adapter import prepare_summary
        owner,subject=self.task_run();raw="가\n".encode()
        matcher=_ExactByteMatcher(b"DONE")
        segments=matcher.feed(raw)+matcher.finish()
        self.assertEqual([(s,len(data)) for s,data,_ in segments],[(0,1),(1,3)])
        owner._record_observed(segments);self.observed_run_raw(owner,raw)
        entries,records=self.confirmed_terminal_records(owner,subject)
        spans=[row["range"] for row in records]
        self.assertEqual([(s["start"],s["end"],s["digest"]) for s in spans],
                         [(s,s+len(data),sha256(data).hexdigest()) for s,data,_ in segments])
        observation={"finding":"UTF8-SPAN-10","raw_bytes_hex":raw.hex(),"original_sources":spans,"records":records}
        try:pieces,gaps=SummarySourceCollector(self.root,lambda:self.sink).collect(owner.repository,subject,records)
        except SummaryDeferred as error:
            observation["observed_defer"]=str(error)
            (EVIDENCE/"review-utf8.json").write_text(json.dumps(observation,ensure_ascii=False,indent=2))
            self.fail("valid continuous Korean output cannot be collected: "+str(error))
        request=self.observed_request(subject,spans)
        prepared=prepare_summary(request,pieces,metadata=owned_metadata(),instructions="existing task")
        text="".join(piece["text"] for piece in prepared["selected"])
        observation.update(prepared_sources=prepared["request"]["sources"],selected_text=text)
        (EVIDENCE/"review-utf8.json").write_text(json.dumps(observation,ensure_ascii=False,indent=2))
        self.assertEqual(gaps,[]);self.assertEqual(prepared["request"]["sources"],spans)
        self.assertEqual(text.count("가\n"),1)

    def test_budget_omits_utf8_continuation_group_without_rewriting_observed_sources(self):
        from workbench.workflow.run import _ExactByteMatcher
        from workbench.backend.summary_adapter import prepare_summary
        owner,subject=self.task_run();raw=("가\n"+"x"*9000).encode()
        matcher=_ExactByteMatcher(b"DONE")
        segments=matcher.feed(raw[:4])+matcher.feed(raw[4:])+matcher.finish()
        owner._record_observed(segments);self.observed_run_raw(owner,raw)
        _,records=self.confirmed_terminal_records(owner,subject)
        originals=deepcopy([record["range"] for record in records])
        self.assertEqual(len(originals),3);self.assertEqual(originals[0]["end"],1)
        pieces,gaps=SummarySourceCollector(self.root,lambda:self.sink).collect(owner.repository,subject,records)
        prepared=prepare_summary(self.observed_request(subject,originals),pieces,metadata=owned_metadata(),instructions="existing task")
        selected=prepared["request"]["sources"];omitted=[item["range"] for item in prepared["omitted"]]
        self.assertEqual(gaps,[])
        self.assertTrue(all(span not in selected and span in omitted for span in originals[:2]),"UTF8 continuation group must be wholly omitted under the actual input quota")
        self.assertEqual(selected,[originals[2]])
        self.assertEqual(sorted(json.dumps(s,sort_keys=True) for s in selected+omitted),sorted(json.dumps(s,sort_keys=True) for s in originals))
        self.assertEqual([record["range"] for record in owner.repository.pending_summary_sources(subject,revision=1)],originals)
        self.assertEqual(prepared["budget"]["inputLimit"],8192)
        (EVIDENCE/"green-utf8-omission.json").write_text(json.dumps({"original_sources":originals,"selected_sources":selected,"omitted":prepared["omitted"],"budget":prepared["budget"]},ensure_ascii=False,indent=2))


# SPEC 8.3 scenarios were fixed before inspecting the implementation: minimal
# dirty interval, allowed closure, physical no-replay, partial raw omission,
# sequence-aware contract protection, and native priority admission.
from copy import deepcopy
import base64
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from workbench.backend.flow import HandoffJournal
from workbench.backend.summary_adapter import SummaryDeferred, SKILL_NAME, skill_bytes
from workbench.backend.summary_scheduler import SummaryScheduler, SummarySourceCollector
from workbench.backend.summary_writer import store_summary
from workbench.contracts.logs_v1 import LogContractError, summary_input_key
from workbench.storage.important_log import SubjectLifecycle
from workbench.storage.log_raw.port import DisabledSegmentedRawPort
from workbench.storage.log_raw.segments import SegmentedRawStore
from workbench.tasks.repository import InvalidTransitionError


def owned_metadata():
    """Controlled supplier fixture; no operational parent epoch claim."""
    return {"source":"parent_session_readback", "epoch":"controlled-private-fixture",
        "parentSessionId":str(uuid4()), "model":{"provider":"scripted", "id":"test",
        "api":"openai-completions", "baseUrl":"http://127.0.0.1:1/v1", "contextWindow":131072,
        "maxTokens":8192}, "thinkingLevel":"off", "serviceTierByFamily":{}, "existingContextTokens":0,
        "outputCap":8192, "reasoningTokens":0, "visibleResultTokens":8192,
        "outputMeaning":"reasoning_and_visible", "outputLimitEnforced":True,
        "outputLimitSource":"controlled-scripted-fixture-not-operating-model-proof", "estimator":"utf8_bytes_upper_bound"}


class CompatibilityPort:
    def __init__(self):
        self.allowed, self.incidents = True, []
    def admission(self): return self.allowed
    def protect(self, incident): self.incidents.append(deepcopy(incident))
    def verify_reference(self, reference): return reference == "explicit-private-scripted-verify"


class OutcomePort:
    """Controlled outcome only; real product writer handles successful results."""
    def __init__(self, root): self.root, self.calls, self.outcome = root, [], None
    def dispatch_outcome(self, prepared, **ports):
        self.calls.append(deepcopy(prepared))
        if self.outcome is not None: return deepcopy(self.outcome)
        result = typed_result(prepared["request"])
        writer = store_summary(self.root, prepared["request"]["requestId"], result)
        if not writer.get("saved"): raise AssertionError(writer)
        return {"status":"summary_result", "sent":True, "protected":False, "result":result,"writer":writer}


def typed_result(request):
    return {"schemaVersion":1, **{k:deepcopy(request[k]) for k in ("requestId","inputKey","skillDigest","sources")},
        "facts":["확인된 작업 내용"], "interpretations":[], "unknowns":[],
        "usage":{"schemaVersion":1,"inputTokens":None,"outputTokens":None,"costUsd":None}}


class SchedulerBoundaryTests(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="scheduler-",dir=EVIDENCE)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.layout=DataLayout(self.root)
        ensure_private_dir(self.layout.workflow/"terminal")
        self.catalog=ImportantLogCatalog(self.layout.important_logs,create=True)
        self.sink=ActiveSubjectLog(self.catalog)
        self.events=HandoffJournal(self.layout.workflow/"handoffs.jsonl"); self.addCleanup(self.events.close)
        self.factory=lambda:TaskRepository(self.layout.tasks)
        self.now=0.;self.paused=False;self.priority=False;self.active=True;self.revoked=False;self.healthy=True;self.dispatch_ready=True
        self.compat=CompatibilityPort();self.adapter=OutcomePort(self.root)
        self.runtime={"metadata":owned_metadata(),"instructions":"existing task prompt fixture","expected_peer":(str(uuid4()),1),"writer":{}}
        self.capture_failures=[]
        self.service=TerminalService.__new__(TerminalService)
        self.service._repository_factory=self.factory;self.service._subject_log=self.sink
        self.service._mark_subject_unknown=lambda:self.capture_failures.append(True)
        self.command,self.subject=self.command_start()
        self.scheduler=self.make_scheduler();self.addCleanup(lambda:self.scheduler.close())
        os.chmod(self.layout.tasks,0o600)

    def command_start(self, *, task=None, revision=None):
        identity=str(uuid4()); path=self.layout.workflow/"terminal"/(identity+".log");path.touch(mode=0o600)
        record=self.events.append({"type":"terminal_started","command_id":identity,"task_id":task,"command":"approved fixture","started_at":AT})
        self.sink.record_terminal(record,revision=revision)
        command=_Command(identity,"fixture",{},task,path);command.summary_revision=revision;command.summary_started=True
        self.service._configure_summary_capture(command)
        subject={"schemaVersion":1,"kind":"task" if task else "command","id":task or identity}
        return command,subject

    def make_scheduler(self, **overrides):
        ports=dict(repository_factory=self.factory,adapter=self.adapter,subject_log=lambda:self.sink,
            collector=SummarySourceCollector(self.root,lambda:self.sink),
            authority_flags=lambda subject,end:{"active":self.active,"revoked":self.revoked},
            paused=lambda:self.paused,priority=lambda:self.priority,runtime=lambda:self.runtime,
            storage_ready=lambda:self.healthy,dispatch_ready=lambda:self.dispatch_ready,compatibility=self.compat,clock=lambda:self.now)
        return SummaryScheduler(**{**ports,**overrides})

    def settle(self):
        until=time.monotonic()+10
        while self.scheduler.status().get("busy") or self.scheduler.status()["inFlight"]:
            if time.monotonic()>until: self.fail(str(self.scheduler.status()))
            time.sleep(.005)
        return self.scheduler.status()

    def cycle(self, now=None):
        if now is not None:self.now=now
        self.scheduler.tick();return self.settle()

    def rows(self):
        with self.factory() as j:
            return [j.get_summary_request(row[0]) for row in j._connection.execute("SELECT request_id FROM summary_requests_v1 ORDER BY rowid")]

    def entries(self, subject=None):
        subject=subject or self.subject
        with self.catalog._lease() as fd:
            with ImportantLogStore.open(self.catalog.directory,self.catalog._current(fd)["active"],readonly=True) as store:
                return [store.get_entry(row[0]) for row in store._connection.execute("SELECT entry_id FROM entries WHERE subject_kind=? AND subject_id=?",(subject["kind"],subject["id"]))]

    def control(self):
        with self.factory() as j:return j.summary_control_state()

    def test_interval_299_300_then_no_health_only_repeat(self):
        self.cycle();self.command.write(b"new work\n");self.cycle(299)
        self.assertEqual(self.adapter.calls,[])
        self.assertEqual(self.cycle(300)["state"],"stored")
        self.assertEqual(len(self.adapter.calls),1)
        self.assertTrue(any(p["text"]=="new work\n" for p in self.adapter.calls[0]["selected"]))
        for t in (301,600,900):self.cycle(t)
        self.assertEqual(len(self.adapter.calls),1)
        self.assertEqual(len([e for e in self.entries() if e["kind"]=="cumulative"]),1)

    def test_end_earlier_than_interval_once_scope_expires(self):
        self.cycle();self.command.write(b"done work")
        self.sink.record_terminal(self.events.append({"type":"terminal_ended","command_id":self.command.command_id,"status":"exited","ended_at":AT}))
        self.active=False
        self.assertEqual(self.cycle(10)["state"],"stored")
        self.assertEqual(len(self.adapter.calls),1)
        self.assertFalse(self.sink.authority(self.subject,active=False,paused=False,revoked=False,has_new_input=True,closing_event=True)["allowed"])
        self.cycle(610);self.assertEqual(len(self.adapter.calls),1)

    def test_pause_priority_storage_and_peer_delivery_gate_send_nothing(self):
        self.cycle()
        for attr in ("paused","priority","healthy","dispatch_ready"):
            old=getattr(self,attr);setattr(self,attr,not old)
            self.cycle(300)
            self.assertEqual(self.adapter.calls,[],attr)
            self.assertEqual(self.rows(),[],attr)
            setattr(self,attr,old)
        self.cycle(600);self.assertEqual(len(self.adapter.calls),1)

    def test_not_sent_is_retried_only_at_next_period_while_sent_unknown_never_replays(self):
        self.cycle();self.adapter.outcome={"status":"deferred","sent":False,"protected":False,"reason":"paused"}
        self.cycle(300);self.assertEqual(self.rows()[0]["state"],"not_sent")
        self.adapter.outcome={"status":"unknown","sent":True,"protected":False,"reason":"provider_lost"}
        self.cycle(599);self.assertEqual(len(self.adapter.calls),1)
        self.cycle(600);self.assertEqual([r["state"] for r in self.rows()],["not_sent","unknown"])
        self.scheduler.close();self.scheduler=self.make_scheduler();self.cycle(900)
        self.assertEqual(len(self.adapter.calls),2)
        with self.factory() as j:self.assertEqual(j.pending_summary_sources(self.subject),[])

    def test_held_job_has_one_inflight_tick_is_nonblocking_valid_late_survives_pause(self):
        self.cycle();entered,release=threading.Event(),threading.Event()
        real=self.adapter.dispatch_outcome
        def held(*args,**kwargs):
            entered.set()
            if not release.wait(4):raise TimeoutError("owner release")
            return real(*args,**kwargs)
        self.adapter.dispatch_outcome=held
        self.now=300;self.scheduler.tick();self.assertTrue(entered.wait(2))
        try:
            job=self.scheduler._job
            t=time.monotonic();self.scheduler.tick();elapsed=time.monotonic()-t
            self.assertLess(elapsed,.1);self.assertIs(self.scheduler._job,job)
            self.paused=True
        finally:release.set()
        self.assertEqual(self.settle()["state"],"stored")
        self.assertEqual(len(self.adapter.calls),1)

    def test_revoke_race_before_claim_reserves_no_new_dispatch(self):
        self.cycle()
        real=self.scheduler.collector.collect
        def collect(*args,**kwargs):
            result=real(*args,**kwargs);self.revoked=True;return result
        self.scheduler.collector.collect=collect
        self.assertEqual(self.cycle(300)["state"],"deferred")
        self.assertEqual(self.rows(),[]);self.assertEqual(self.adapter.calls,[])

    def test_contract_streak_survives_transient_storage_restart_and_recovery_rejects_once(self):
        self.cycle();self.adapter.outcome={"status":"rejected","sent":True,"protected":True,"reason":"invalid_result"}
        self.cycle(300);self.assertEqual(self.control()["streak"],1);self.assertIsNone(self.control()["protection"])
        for t,outcome in ((600,{"status":"unknown","sent":True,"protected":False,"reason":"provider_error"}),
                          (900,{"status":"storage_failed","sent":True,"protected":False,"reason":"disk_full"})):
            self.command.write(f"work{t}".encode());self.adapter.outcome=outcome;self.cycle(self.now);self.cycle(t)
            self.assertEqual(self.control()["streak"],1)
        self.adapter.outcome={"status":"rejected","sent":True,"protected":True,"reason":"invalid_result"}
        for t in (1200,1500):self.command.write(f"work{t}".encode());self.cycle(self.now);self.cycle(t)
        self.assertEqual(self.control()["streak"],3);self.assertEqual(self.control()["protection"],"summary_contract_failed")
        self.assertEqual(len(self.compat.incidents),1)
        old_episode=self.control()["episode"]
        self.scheduler.close();self.scheduler=self.make_scheduler();self.cycle(1800)
        self.assertEqual(self.control()["episode"],old_episode);self.assertEqual(len(self.compat.incidents),1)
        with self.assertRaisesRegex(SummaryDeferred,"verified_boundary"):
            self.scheduler.recover("unverified")
        attempt=self.scheduler.recover("explicit-private-scripted-verify")
        self.assertTrue(attempt["plumbing_verified"]);self.assertEqual(attempt["real_model_contract"],"unverified")
        self.command.write(b"new never-sent recovery work");self.cycle(1800);self.cycle(2100)
        self.assertEqual(self.control()["protection"],"summary_contract_failed")
        self.assertNotEqual(self.control()["episode"],old_episode)
        self.assertEqual(len(self.compat.incidents),2)

    def test_safety_rejection_protects_once_without_three_result_failures(self):
        self.cycle();self.adapter.outcome={"status":"rejected","sent":True,"protected":True,"reason":"guard_violation_observed"}
        self.cycle(300);self.assertEqual(self.control()["protection"],"guard_violation_observed")
        self.cycle(600);self.assertEqual(len(self.adapter.calls),1);self.assertEqual(len(self.compat.incidents),1)

    def test_budget_local_protected_outcomes_preserve_global_streak_and_physical_retry_boundary(self):
        self.adapter.outcome={"status":"rejected","sent":True,"protected":True,"reason":"invalid_result"}
        self.cycle();self.cycle(300)
        self.assertEqual(self.control()["streak"],1)
        observations=[]
        for reason in ("input_budget_unavailable","output_budget_unavailable"):
            for status,sent,expected in (("deferred",False,"not_sent"),("rejected",True,"rejected"),("unknown",True,"unknown")):
                with self.subTest(reason=reason,status=status):
                    self.command.write((reason+status).encode())
                    self.cycle(self.now)
                    self.adapter.outcome={"status":status,"sent":sent,"protected":True,"reason":reason}
                    self.cycle(self.now+300)
                    request=self.adapter.calls[-1]["request"]
                    with self.factory() as journal:
                        row=journal.get_summary_request(request["requestId"])
                        self.assertEqual(row["state"],expected);self.assertIsNone(row["receipt"])
                        detail=journal._connection.execute("SELECT detail FROM summary_request_events_v1 WHERE request_id=? ORDER BY event_id DESC LIMIT 1",(request["requestId"],)).fetchone()[0]
                        self.assertEqual(detail,reason)
                        replay=deepcopy(request);replay["requestId"]=str(uuid4())
                        if sent:
                            with self.assertRaises(InvalidTransitionError):journal.reserve_summary_request(replay)
                        else:
                            journal.reserve_summary_request(replay)
                            journal.record_summary_state(replay["requestId"],"not_sent",detail="confirmed independent retry admission, no dispatch")
                    before=self.control()
                    self.assertEqual(before["streak"],1);self.assertIsNone(before["protection"]);self.assertIsNone(before["episode"])
                    self.assertEqual(before["last_reason"],reason);self.assertEqual(self.compat.incidents,[])
                    self.scheduler.close();self.scheduler=self.make_scheduler()
                    self.assertEqual(self.control()["streak"],1);self.assertIsNone(self.control()["protection"])
                    self.assertEqual(self.compat.incidents,[])
                    self.assertFalse(any(entry["kind"]=="cumulative" for entry in self.entries()))
                    observations.append({"reason":reason,"outcome":status,"journal_state":expected,"sent":sent,"control":before,"receipt":None})
        (EVIDENCE/"budget-live-outcomes.json").write_text(json.dumps(observations,ensure_ascii=False,indent=2))

    def test_failure_does_not_overwrite_last_good_and_duplicate_late_receipt_is_drop(self):
        self.cycle();self.cycle(300)
        first=self.rows()[0]
        with self.factory() as j:
            last=j.latest_summary_receipt(self.subject)
            self.assertFalse(j.record_summary_result_receipt(typed_result(first["request"]),last["entry_id"]))
        self.command.write(b"new incomplete output")
        self.adapter.outcome={"status":"storage_failed","sent":True,"protected":False,"reason":"disk_full"}
        self.cycle(600)
        with self.factory() as j:self.assertEqual(j.latest_summary_receipt(self.subject),last)
        self.assertEqual(len([e for e in self.entries() if e["kind"]=="cumulative"]),1)

    def test_latest_valid_late_receipt_resets_control_on_ordinary_tick_without_restart(self):
        self.adapter.outcome={"status":"rejected","sent":True,"protected":True,"reason":"invalid_result"}
        for index in range(3):
            self.command.write(("new contract observation "+str(index)).encode())
            self.cycle(self.now);self.cycle(self.now+300)
        self.assertEqual(self.control()["streak"],3)
        attempt=self.scheduler.recover("explicit-private-scripted-verify")
        self.command.write(b"new late valid recovery observation")
        self.adapter.outcome={"status":"unknown","sent":True,"protected":False,"reason":"provider_timeout"}
        self.cycle(self.now);self.cycle(self.now+300)
        request=self.adapter.calls[-1]["request"]
        self.assertFalse(self.scheduler.status()["inFlight"])
        with self.factory() as journal:self.assertEqual(journal.get_summary_request(request["requestId"])["state"],"unknown")
        before=self.control();self.assertEqual(before["recovery_attempt"],attempt)
        writer=store_summary(self.root,request["requestId"],typed_result(request))
        self.assertTrue(writer["saved"],writer)
        with self.factory() as journal:self.assertEqual(journal.get_summary_request(request["requestId"])["state"],"stored")
        count=len(self.adapter.calls)
        self.cycle(self.now) # no constructor/restart/in-flight outcome or explicit stored control
        after=self.control()
        (EVIDENCE/"review-late-control.json").write_text(json.dumps({"finding":"LATE-STORED-CONTROL-10","request":request,
            "before":before,"writer":writer,"after_ordinary_tick":after,"dispatch_before":count,"dispatch_after":len(self.adapter.calls)},ensure_ascii=False,indent=2))
        self.assertEqual(len(self.adapter.calls),count)
        self.assertEqual(after["streak"],0,"latest stored receipt must reset P5 without scheduler restart")
        self.assertIsNone(after["recovery_attempt"])
        self.command.write(b"live sending remains in flight on ordinary ticks")
        self.cycle(self.now)
        with self.factory() as journal:
            pending=journal.pending_summary_sources(self.subject)
            span=max((row["range"] for row in pending if row["range"]["source"]["sourceKind"]=="terminal"),key=lambda value:value["start"])
            live=deepcopy(request);live["requestId"]=str(uuid4());live["sources"]=[span]
            live["inputKey"]=summary_input_key(self.subject,None,[span],[],live["skillDigest"])
            journal.reserve_summary_request(live);journal.record_summary_state(live["requestId"],"sending")
            stored_count=journal._connection.execute("SELECT COUNT(*) FROM summary_control_events_v1 WHERE request_id=? AND outcome='stored'",(request["requestId"],)).fetchone()[0]
        self.cycle(self.now);self.cycle(self.now)
        with self.factory() as journal:self.assertEqual(journal.get_summary_request(live["requestId"])["state"],"sending")
        self.scheduler.close();self.scheduler=self.make_scheduler()
        with self.factory() as journal:
            self.assertEqual(journal.get_summary_request(live["requestId"])["state"],"unknown")
            self.assertEqual(journal._connection.execute("SELECT COUNT(*) FROM summary_control_events_v1 WHERE request_id=? AND outcome='stored'",(request["requestId"],)).fetchone()[0],stored_count)
        self.assertEqual(stored_count,1)
        (EVIDENCE/"green-late-idempotence.json").write_text(json.dumps({"latest_stored_request":request["requestId"],"stored_control_rows":stored_count,"ordinary_tick_live_state":"sending","restart_live_state":"unknown"},indent=2))

    def test_observed_sdk_provider_error_remains_transient_and_no_replay_after_restart(self):
        output=(EVIDENCE/"review-provider-sdk-outcome.json").resolve()
        env={**os.environ,"DVL10_REVIEW_PROVIDER_OUTCOME":str(output)}
        command=["node","--test","--test-name-pattern=provider error shape for Python journal regression",
                 "tests/bridge/summary_sdk_budget_independent.test.ts"]
        process=subprocess.run(command,cwd=Path(__file__).resolve().parents[2],env=env,capture_output=True,text=True,timeout=15)
        (EVIDENCE/"review-provider-node.log").write_text(process.stdout+process.stderr)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        observed=json.loads(output.read_text());self.assertTrue(observed["outcome"]["sent"])
        self.assertFalse(observed["outcome"]["protected"])
        self.assertEqual(observed["outcome"]["status"],"unknown")
        self.assertEqual(observed["outcome"]["reason"],"summary_outcome_unknown")
        self.adapter.outcome={"status":"rejected","sent":True,"protected":True,"reason":"invalid_result"}
        self.cycle();self.cycle(300);self.assertEqual(self.control()["streak"],1)
        self.command.write(b"fresh work before transient provider failure")
        self.cycle(self.now);self.adapter.outcome=observed["outcome"];self.cycle(self.now+300)
        request=self.adapter.calls[-1]["request"];before=self.control()
        self.scheduler.close();self.scheduler=self.make_scheduler()
        after=self.control()
        with self.factory() as journal:
            row=journal.get_summary_request(request["requestId"])
            replay=deepcopy(request);replay["requestId"]=str(uuid4())
            with self.assertRaises(InvalidTransitionError):journal.reserve_summary_request(replay)
        (EVIDENCE/"review-transient-control.json").write_text(json.dumps({"finding":"SDK-TRANSIENT-SAFETY-10",
            "sdk":observed,"before_restart":before,"after_restart":after,"journal":row,"incidents":self.compat.incidents},ensure_ascii=False,indent=2))
        self.assertIsNone(before["protection"],"a provider failure with no safety evidence must not protect summaries")
        self.assertEqual(before["streak"],1);self.assertIsNone(after["protection"])
        self.assertEqual(after["streak"],1);self.assertEqual(self.compat.incidents,[])
        self.assertIsNone(row["receipt"])

    def test_observed_sdk_protected_unknown_keeps_immediate_safety_after_restart(self):
        output=(EVIDENCE/"protected-unknown-sdk-outcome.json").resolve()
        env={**os.environ,"DVL10_PROTECTED_UNKNOWN_OUTCOME":str(output)}
        command=["node","--test","--test-name-pattern=confirmed forbidden end with abort rejection shape for Python protection regression",
                 "tests/bridge/summary_sdk_budget_independent.test.ts"]
        process=subprocess.run(command,cwd=Path(__file__).resolve().parents[2],env=env,capture_output=True,text=True,timeout=15)
        (EVIDENCE/"protected-unknown-node.log").write_text(process.stdout+process.stderr)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        observed=json.loads(output.read_text())
        self.assertEqual(observed["outcome"],{"status":"unknown","sent":True,"protected":True,"reason":"compatibility_failed"})
        self.assertEqual(observed["counters"],{"dispatch":1,"write":0,"abort":1,"dispose":1})
        self.adapter.outcome=observed["outcome"]
        self.cycle();self.cycle(300)
        request=self.adapter.calls[-1]["request"];immediate=self.control()
        incidents_immediate=deepcopy(self.compat.incidents)
        with self.factory() as journal:
            unknown=journal.get_summary_request(request["requestId"])
            replay=deepcopy(request);replay["requestId"]=str(uuid4())
            with self.assertRaises(InvalidTransitionError):journal.reserve_summary_request(replay)
            control_count=lambda:journal._connection.execute("SELECT COUNT(*) FROM summary_control_events_v1 WHERE request_id=?",(request["requestId"],)).fetchone()[0]
            count_immediate=control_count()
        self.cycle(self.now);self.cycle(self.now)
        ordinary=self.control()
        with self.factory() as journal:
            count_ordinary=journal._connection.execute("SELECT COUNT(*) FROM summary_control_events_v1 WHERE request_id=?",(request["requestId"],)).fetchone()[0]
        self.scheduler.close();self.scheduler=self.make_scheduler()
        restarted=self.control()
        with self.factory() as journal:
            restarted_unknown=journal.get_summary_request(request["requestId"])
            with self.assertRaises(InvalidTransitionError):journal.reserve_summary_request(replay)
            count_restart=journal._connection.execute("SELECT COUNT(*) FROM summary_control_events_v1 WHERE request_id=?",(request["requestId"],)).fetchone()[0]
            self.assertEqual(journal.summary_unclassified_outcomes(),[],"restart/helper and durable SQL classification must agree")
        dispatch_before=len(self.adapter.calls)
        self.command.write(b"new work must wait after confirmed safety violation")
        self.cycle(self.now);self.cycle(self.now+300)
        final=self.control()
        (EVIDENCE/"protected-unknown-control.json").write_text(json.dumps({"finding":"PROTECTED-UNKNOWN-SAFETY-10",
            "sdk":observed,"request":request,"immediate":immediate,"ordinary":ordinary,"restart":restarted,"final":final,
            "journal_unknown":unknown,"journal_after_restart":restarted_unknown,
            "control_counts":[count_immediate,count_ordinary,count_restart],"incidents_immediate":incidents_immediate,
            "incidents_final":self.compat.incidents,"dispatch_before_new_input":dispatch_before,"dispatch_after_new_input":len(self.adapter.calls),
            "same_physical_range_reserve_rejected_before_and_after_restart":True,
            "cumulative_entries":len([entry for entry in self.entries() if entry["kind"]=="cumulative"])},ensure_ascii=False,indent=2))
        self.assertEqual(immediate["protection"],"compatibility_failed","confirmed safety cannot become transient because SDK completion is unknown")
        self.assertEqual(len(incidents_immediate),1)
        self.assertEqual(unknown["state"],"unknown");self.assertIsNone(unknown["receipt"])
        self.assertEqual(restarted_unknown["state"],"unknown");self.assertIsNone(restarted_unknown["receipt"])
        for state in (ordinary,restarted,final):
            self.assertEqual(state["protection"],"compatibility_failed")
            self.assertEqual(state["episode"],immediate["episode"])
        self.assertEqual([count_immediate,count_ordinary,count_restart],[1,1,1])
        self.assertEqual(len(self.compat.incidents),1)
        self.assertEqual(len(self.adapter.calls),dispatch_before)
        self.assertFalse(any(entry["kind"]=="cumulative" for entry in self.entries()))
        parity=[]
        with self.factory() as journal:
            for reason in ("summary_outcome_unknown","input_budget_unavailable","invalid_result","owner_inactive"):
                # Classification-table parity at existing journal APIs, no new SDK faults.
                self.command.write(("fresh observed parity source "+reason).encode())
                for entry in self.entries():
                    for span in entry["sources"]:
                        journal.ingest_summary_source(self.subject,span,entry["entryId"])
                pending=journal.pending_summary_sources(self.subject)
                span=max((row["range"] for row in pending if row["range"]["source"]["sourceKind"]=="terminal"),key=lambda value:value["start"])
                item=deepcopy(request);item["requestId"]=str(uuid4());item["sources"]=[span]
                item["inputKey"]=summary_input_key(self.subject,None,[span],[],item["skillDigest"])
                journal.reserve_summary_request(item);journal.record_summary_state(item["requestId"],"sending")
                journal.record_summary_state(item["requestId"],"unknown",detail=reason)
                before=journal.summary_control_state()
                self.assertEqual(journal.summary_outcome_kind("unknown",reason),"transient")
                self.assertIsNone(journal.record_summary_control(item["requestId"],"transient",reason=reason))
                after=journal.summary_control_state()
                self.assertEqual(journal.summary_unclassified_outcomes(),[])
                self.assertEqual(after["streak"],before["streak"])
                self.assertEqual(after["protection"],before["protection"])
                self.assertEqual(after["episode"],before["episode"])
                parity.append({"reason":reason,"kind":"transient","before":before,"after":after,"unclassified":[]})
        (EVIDENCE/"protected-unknown-transient-parity.json").write_text(json.dumps(parity,ensure_ascii=False,indent=2))

    def test_successful_sdk_prompt_without_result_is_three_strike_contract_rejection(self):
        output=(EVIDENCE/"green-empty-sdk-outcome.json").resolve()
        env={**os.environ,"DVL10_REVIEW_PROVIDER_OUTCOME":str(output)}
        command=["node","--test","--test-name-pattern=successful empty prompt shape for Python journal regression",
                 "tests/bridge/summary_sdk_budget_independent.test.ts"]
        process=subprocess.run(command,cwd=Path(__file__).resolve().parents[2],env=env,capture_output=True,text=True,timeout=15)
        (EVIDENCE/"green-empty-node.log").write_text(process.stdout+process.stderr)
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        observed=json.loads(Path(str(output)+"-empty.json").read_text())
        self.assertEqual(observed["outcome"],{"status":"rejected","sent":True,"protected":False,"reason":"result_unavailable"})
        self.adapter.outcome=observed["outcome"]
        for index in range(3):
            self.command.write(("new empty result source "+str(index)).encode())
            self.cycle(self.now);self.cycle(self.now+300)
            state=self.control();self.assertEqual(state["streak"],index+1)
            self.assertEqual(state["protection"],"summary_contract_failed" if index==2 else None)
        self.assertEqual(len(self.compat.incidents),1)
        self.assertFalse(any(e["kind"]=="cumulative" for e in self.entries()))
        (EVIDENCE/"green-empty-contract.json").write_text(json.dumps({"sdk":observed,"control":self.control(),"incidents":self.compat.incidents},ensure_ascii=False,indent=2))

    def test_task_bound_command_is_one_task_request_and_closed_cancel_collects_only(self):
        with self.factory() as j:
            task=j.create_task({"goal":"Task ownership"});j.approve_scope(task,1,{"paths":["fixture"]});j.proceed(task,1,"approved")
        delegated={"seq":91,"at":AT,"type":"delegated","task_id":task,"revision":1,
            "scope_decision_id":str(uuid4()),"proceed_decision_id":str(uuid4())}
        flow_path=self.layout.workflow/"tasks-flow.jsonl"
        flow_path.write_text(json.dumps(delegated)+"\n");os.chmod(flow_path,0o600)
        self.sink.record_flow(delegated)
        command,subject=self.command_start(task=task,revision=1);command.write(b"Task exact output")
        self.cycle();self.cycle(300);self.cycle(300)
        task_calls=[p for p in self.adapter.calls if p["request"]["subject"]==subject]
        self.assertEqual(len(task_calls),1);self.assertEqual(task_calls[0]["request"]["revision"],1)
        bound=lambda span:span["source"].get("commandId")==command.command_id
        raw_selected=any(bound(s) for s in task_calls[0]["request"]["sources"])
        if not raw_selected:
            self.assertTrue(any(bound(item["range"]) and item["reason"]=="input_budget" for item in task_calls[0]["omitted"]))
            self.cycle(600)
            task_calls=[p for p in self.adapter.calls if p["request"]["subject"]==subject]
            self.assertTrue(any(bound(span) for call in task_calls[1:] for span in call["request"]["sources"]),
                            "pending omitted command span must remain available for subsequent budgeted period")
        self.assertFalse(any(p["request"]["subject"]["id"]==command.command_id for p in self.adapter.calls))
        before=len(task_calls)
        self.sink.record_flow({"seq":92,"at":AT,"type":"task_closed","task_id":task,"reason":"cancelled"})
        self.active=False;self.cycle(601);self.assertEqual(len([p for p in self.adapter.calls if p["request"]["subject"]==subject]),before)

    def test_lifecycle_active_parity_and_foreign_gap_or_stale_epoch_reject(self):
        self.command.write(b"known work")
        self.cycle();self.cycle(300)
        request=self.adapter.calls[0]["request"]
        life=SubjectLifecycle(self.catalog.directory,self.catalog.health_snapshot()["active"]["dbUuid"],_catalog_owned=True)
        for changed in ({},{"paused":True},{"revoked":True},{"active":False}):
            flags={"active":True,"paused":False,"revoked":False,"has_new_input":True,**changed}
            self.assertEqual(self.sink.authority(self.subject,**flags),life.authority(self.subject,**flags))
        flags={"active":True,"paused":False,"revoked":False,"has_new_input":True}
        stale=deepcopy(request);stale["authorizationEpoch"]+=1
        self.assertFalse(self.sink.authorize_summary_request(stale,**flags)["allowed"])
        foreign=deepcopy(request)
        foreign["sources"]=[];foreign["sourceGaps"]=[{"schemaVersion":1,"source":{"schemaVersion":1,"sourceKind":"terminal","collectorEpoch":1,"commandId":str(uuid4())},"start":0,"end":10,"reason":"storage_gap"}]
        foreign["inputKey"]=summary_input_key(self.subject,None,[],foreign["sourceGaps"],foreign["skillDigest"])
        with self.assertRaises(LogContractError):self.sink.authorize_summary_request(foreign,**flags)
        unrelated=deepcopy(request);span=next(s for s in request["sources"] if s["source"]["sourceKind"]=="terminal")
        unrelated["sources"]=[];unrelated["sourceGaps"]=[{"schemaVersion":1,"source":span["source"],"start":10,"end":11,"reason":"storage_gap"}]
        unrelated["inputKey"]=summary_input_key(self.subject,None,[],unrelated["sourceGaps"],unrelated["skillDigest"])
        self.assertFalse(self.sink.authorize_summary_request(unrelated,**flags)["allowed"])

    def test_task_close_reason_table_matches_lifecycle_and_no_notice_grant(self):
        for n,reason in enumerate(("done","report_outcome_unknown","report_not_delivered","cancelled","approval_retired_c_d66","not_started_before_restart")):
            subject={"schemaVersion":1,"kind":"task","id":str(uuid4())}
            self.sink.record_flow({"seq":100+n*2,"at":AT,"type":"delegated","task_id":subject["id"],"revision":1,"scope_decision_id":str(uuid4()),"proceed_decision_id":str(uuid4())})
            self.sink.record_flow({"seq":101+n*2,"at":AT,"type":"task_closed","task_id":subject["id"],"reason":reason})
            flags={"active":False,"paused":False,"revoked":False,"has_new_input":True,"closing_event":True}
            life=SubjectLifecycle(self.catalog.directory,self.catalog.health_snapshot()["active"]["dbUuid"],_catalog_owned=True)
            decision=self.sink.authority(subject,**flags)
            self.assertEqual(decision,life.authority(subject,**flags))
            self.assertEqual(decision["allowed"],n<3,reason)
            self.assertFalse(self.sink.authority(subject,**{**flags,"revoked":True})["allowed"])
        unknown={"schemaVersion":1,"kind":"command","id":str(uuid4())}
        self.assertFalse(self.sink.authority(unknown,active=True,paused=False,revoked=False,has_new_input=True)["allowed"])

    def test_native_automation_terminal_priority_and_g3_delivery_admission(self):
        from workbench.backend.automation import AutomationController
        from workbench.backend.service import Backend
        from workbench.ipc.bridge_g3.mailbox import G3BridgeServer
        from tests.backend.test_automation_loop import ScriptedPeer
        socket_dir=Path.cwd()/".workflow/dvl-local"/("s10-"+uuid4().hex[:8]);socket_dir.mkdir(mode=0o700)
        socket_path=socket_dir/"bridge.sock"
        self.assertLessEqual(len(os.fsencode(str(socket_path))),107)
        self.assertEqual(socket_dir.stat().st_uid,os.getuid());self.assertEqual(socket_dir.stat().st_mode&0o777,0o700)
        tokens={role:str(uuid4()) for role in ("manager","worker")}
        bridge=G3BridgeServer(socket_path,tokens);bridge.start();peer=None
        try:
            peer=ScriptedPeer(bridge,"manager",tokens["manager"])
            automation=AutomationController.__new__(AutomationController)
            automation._lock=threading.RLock();automation._transition=None
            automation._bound=SimpleNamespace(kind="experiment",ended=False,host_finished=False)
            automation._last_review={"pending":True,"next_due_at":60};automation._clock=lambda:self.now
            terminal=TerminalService.__new__(TerminalService);terminal._lock=threading.RLock()
            terminal._current=self.command;terminal._noticing=[];terminal._clock=lambda:self.now;terminal._check_interval=60
            backend=Backend.__new__(Backend);backend.bridge=bridge;backend.automation_loop=automation;backend.terminal=terminal;backend._hold_reason=lambda role:None
            self.scheduler.priority=lambda:backend._summary_priority()
            self.scheduler.dispatch_ready=backend._summary_dispatch_ready
            self.cycle();self.cycle(300);self.assertEqual(self.adapter.calls,[])
            automation._last_review={"pending":False,"next_due_at":360}
            self.command.check_base=300
            self.command.notice="pending";terminal._noticing=[self.command]
            self.cycle(300);self.assertEqual(self.adapter.calls,[])
            self.command.notice="none";terminal._noticing=[]
            peer.idle=False;self.cycle(300);self.assertEqual(self.adapter.calls,[]);self.assertEqual(self.rows(),[])
            peer.idle=True
            lock=bridge.delivery_lock("manager");lock.acquire()
            try:self.cycle(300);self.assertEqual(self.adapter.calls,[])
            finally:lock.release()
            # Confirmed no-send execution advances the regular due time; freeing
            # a delivery lock is not permission to dispatch an emergency call.
            automation._last_review={"pending":False,"next_due_at":660}
            self.command.check_base=600
            self.assertEqual(self.cycle(600)["state"],"stored")
            self.assertEqual(len(self.adapter.calls),1)
        finally:
            if peer is not None:peer.close()
            bridge.close();socket_dir.rmdir()

    def partial_run(self, mode):
        # A-prime: confirmed original span remains original producer evidence.
        # Missing subranges consume it after the request; available unverified
        # fragments must never be promoted into new evidence or prompt text.
        command=self.command;command.echo=b"[worker] $ fixture\r\n"
        if mode=="legacy":
            with patch("workbench.backend.flow_terminal.LOG_MAX",4):
                command.write(b"[worker] $ fi");command.write(b"xture\r\nUNVERIFIED")
            expected=[(4,10,"storage_gap")]
        elif mode=="missing":
            command.log_path.unlink();command.write(b"UNVERIFIED")
            expected=[(0,10,"storage_gap")]
        else:
            storage_gap=mode=="segmented_storage"
            store=SegmentedRawStore(self.root/"raw-v2",segment_limit=1024 if storage_gap else 512,segment_count=5 if storage_gap else 1)
            self.addCleanup(store.close);port=DisabledSegmentedRawPort(store)
            path,descriptor=port.prepare_command(command.log_path.parent,command.command_id)
            command.log_path,command.raw_descriptor,command.segmented_raw_port=path,descriptor,port
            command.echo=b""
            payload=b"UNVERIFIED"*(500 if storage_gap else 160)
            if storage_gap:
                actual_append=store._append_frame
                frames=[]
                def append_then_no_space(*args,**kwargs):
                    frames.append(True)
                    if len(frames)>1:raise OSError(28,"owned fixture raw storage full")
                    return actual_append(*args,**kwargs)
                with patch.object(store,"_append_frame",side_effect=append_then_no_space):command.write(payload)
                self.assertGreater(len(frames),1)
            else:command.write(payload)
            raw=port.read(descriptor,0,len(payload))
            self.assertTrue(raw["read"]["chunks"]);self.assertTrue(raw["read"]["missing"])
            expected=[(g["start"],g["end"],g["reason"]) for g in raw["read"]["missing"]]
            self.assertTrue(all(reason==("storage_gap" if storage_gap else "evicted") for _,_,reason in expected))
            self.scheduler.collector.segmented=port
        self.cycle();self.assertEqual(self.cycle(300)["state"],"stored")
        request=self.adapter.calls[0]["request"]
        self.assertEqual([(g["start"],g["end"],g["reason"]) for g in request["sourceGaps"]],expected)
        self.assertFalse(any(s["source"]["sourceKind"]=="terminal" for s in request["sources"]))
        self.assertNotIn("UNVERIFIED",str(self.adapter.calls[0]["selected"]))
        if mode!="missing":self.assertTrue(any("생략" in text for e in self.entries() for text in e["body"]["unknowns"]))
        count=len(self.entries())
        with self.factory() as j:self.assertEqual(j.pending_summary_sources(self.subject),[])
        self.cycle(600);self.cycle(900)
        self.assertEqual(len(self.adapter.calls),1);self.assertEqual(len(self.entries()),count)

    def test_legacy_logmax_suffix_is_exact_gap_without_retained_fragment_prompt(self):self.partial_run("legacy")
    def test_missing_raw_is_exact_gap_and_does_not_stall_subject(self):self.partial_run("missing")
    def test_real_segment_evicted_prefix_keeps_exact_gap_and_consumes_original(self):self.partial_run("segmented")
    def test_real_segment_storage_suffix_keeps_exact_gap_and_no_partial_text(self):self.partial_run("segmented_storage")

    def test_echo_spans_digest_duplicates_foreign_attribution_and_tampering(self):
        self.command.echo=b"[worker] $ fixture\r\n"
        self.command.write(b"[worker] $ fi");self.command.write(b"xture\r\nfirst\n");self.command.write(b"second\n")
        with self.factory() as j:
            rows=[r for r in j.get_log_sources(self.subject) if r["source"]["sourceKind"]=="terminal"]
        spans=[{ "schemaVersion":1,**{k:r[k] for k in ("source","start","end","digest")}} for r in rows]
        self.assertEqual([(s["start"],s["end"],s["digest"]) for s in spans],[(0,6,sha256(b"first\n").hexdigest()),(6,13,sha256(b"second\n").hexdigest())])
        self.assertEqual(self.command.log_path.read_bytes(),b"first\nsecond\n")
        self.assertFalse(self.sink.record_terminal_range(self.subject,rows[0]["source_id"],spans[0],observed_at=AT))
        with self.assertRaises(ValueError):
            self.sink.record_terminal_range({**self.subject,"id":str(uuid4())},str(uuid4()),spans[0],observed_at=AT)
        self.cycle();self.command.log_path.write_bytes(b"wrong\nsecond\n");os.chmod(self.command.log_path,0o600)
        self.assertEqual(self.cycle(300)["reason"],"source_digest_mismatch");self.assertEqual(self.adapter.calls,[])


class DurableControlBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="control-",dir=EVIDENCE);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.j=TaskRepository(self.root/"tasks.sqlite3");self.addCleanup(lambda:self.j.close())
        self.subject={"schemaVersion":1,"kind":"command","id":str(uuid4())};self.counter=0

    def reserve(self, *, sending=True):
        self.counter+=1;source={"schemaVersion":1,"sourceKind":"terminal","collectorEpoch":1,"commandId":self.subject["id"]}
        span={"schemaVersion":1,"source":source,"start":self.counter*4,"end":self.counter*4+4,"digest":sha256(b"work").hexdigest()}
        request={"schemaVersion":1,"requestId":str(uuid4()),"subject":self.subject,"revision":None,
            "authorizationRef":str(uuid4()),"authorizationEpoch":1,"skillName":SKILL_NAME,"skillDigest":sha256(skill_bytes()).hexdigest(),
            "sources":[span],"sourceGaps":[],"priorCumulativeEntryId":None,"dbHealth":{"dbUuid":str(uuid4()),"quickCheck":"ok","checkedAt":AT},"requestedAt":AT}
        # Reuse the actual catalog health shape, rather than guessing required fields.
        catalog=ImportantLogCatalog(self.root/"logs",create=True);request["dbHealth"]=catalog.health_snapshot()["active"]
        request["inputKey"]=summary_input_key(self.subject,None,request["sources"],[],request["skillDigest"])
        self.j.reserve_summary_request(request)
        if sending:self.j.record_summary_state(request["requestId"],"sending")
        return request

    def reject(self):
        request=self.reserve();rid=request["requestId"]
        self.j.record_summary_state(rid,"executed_observed");self.j.record_summary_state(rid,"rejected",detail="invalid_result")
        return self.j.record_summary_control(rid,"contract_rejected",reason="invalid_result")

    def test_old_late_cannot_clear_streak_or_recovery_latest_stored_can(self):
        old=self.reserve()
        for _ in range(3):self.reject()
        attempt=self.j.begin_summary_recovery("explicit-verified",boundaries_verified=True)
        self.j.record_summary_result_receipt(typed_result(old),str(uuid4()));self.j.record_summary_control(old["requestId"],"stored",reason="stored")
        self.assertEqual(self.j.summary_control_state()["recovery_attempt"],attempt)
        latest=self.reserve();self.j.record_summary_control(latest["requestId"],"storage",reason="storage")
        self.assertEqual(self.j.summary_control_state()["recovery_attempt"],attempt)
        self.j.record_summary_result_receipt(typed_result(latest),str(uuid4()));self.j.record_summary_control(latest["requestId"],"stored",reason="stored")
        self.assertEqual(self.j.summary_control_state()["streak"],0);self.assertIsNone(self.j.summary_control_state()["recovery_attempt"])

    def test_restart_raw_budget_reason_keeps_recovery_and_no_replay_without_safety_episode(self):
        for _ in range(3):self.reject()
        recovery=self.j.begin_summary_recovery("explicit-verified",boundaries_verified=True)
        before=self.j.summary_control_state()
        requests=[]
        for reason in ("input_budget_unavailable","output_budget_unavailable"):
            for state in ("not_sent","rejected","unknown"):
                request=self.reserve();rid=request["requestId"]
                if state=="rejected":self.j.record_summary_state(rid,"executed_observed")
                self.j.record_summary_state(rid,state,detail=reason)
                requests.append((reason,state,request))
        self.j.close();self.j=TaskRepository(self.root/"tasks.sqlite3")
        compatibility=CompatibilityPort()
        observer=SummaryScheduler(repository_factory=lambda:TaskRepository(self.root/"tasks.sqlite3"),adapter=None,
            subject_log=lambda:None,collector=SummarySourceCollector(self.root,lambda:None),
            authority_flags=lambda subject,end:{},paused=lambda:True,priority=lambda:False,compatibility=compatibility)
        try:
            state=self.j.summary_control_state()
            self.assertEqual(state["streak"],3);self.assertEqual(state["recovery_attempt"],recovery)
            self.assertEqual(state["episode"],before["episode"]);self.assertIsNone(state["protection"])
            self.assertEqual(compatibility.incidents,[])
            for reason,status,request in requests:
                self.assertEqual(self.j.get_summary_request(request["requestId"])["state"],status)
                rows=self.j._connection.execute("SELECT outcome,state_json FROM summary_control_events_v1 WHERE request_id=?",(request["requestId"],)).fetchall()
                if status=="not_sent":self.assertEqual(rows,[])
                else:
                    self.assertEqual([row[0] for row in rows],["transient"])
                    self.assertEqual(json.loads(rows[0][1])["last_reason"],reason)
                replay=deepcopy(request);replay["requestId"]=str(uuid4())
                if status=="not_sent":
                    self.j.reserve_summary_request(replay)
                    self.j.record_summary_state(replay["requestId"],"not_sent",detail="confirmed no dispatch")
                else:
                    with self.assertRaises(InvalidTransitionError):self.j.reserve_summary_request(replay)
            (EVIDENCE/"budget-restart-state.json").write_text(json.dumps({"before":before,"after":state,"cases":[{"reason":r,"state":s,"requestId":q["requestId"]} for r,s,q in requests],"incidents":compatibility.incidents},ensure_ascii=False,indent=2))
        finally:observer.close()

    def test_physical_no_replay_survives_new_epoch_skill_requestid(self):
        request=self.reserve();self.j.record_summary_state(request["requestId"],"unknown")
        changed=deepcopy(request);changed["requestId"]=str(uuid4());changed["authorizationEpoch"]+=1
        changed["sources"][0]["source"]["collectorEpoch"]+=1;changed["skillDigest"]="f"*64
        changed["inputKey"]=summary_input_key(changed["subject"],None,changed["sources"],[],changed["skillDigest"])
        with self.assertRaises(InvalidTransitionError):self.j.reserve_summary_request(changed)

    def test_restart_converts_only_prepared_to_not_sent_and_sending_to_unknown(self):
        requests={}
        for state in ("prepared","sending","executed_observed","unknown","rejected","storage_failed","stored","not_sent"):
            request=self.reserve(sending=state!="prepared");rid=request["requestId"];requests[state]=request
            if state in ("executed_observed","unknown","not_sent"):
                self.j.record_summary_state(rid,state,detail="owned pre-send refusal confirmed" if state=="not_sent" else None)
            elif state in ("rejected","storage_failed"):
                self.j.record_summary_state(rid,"executed_observed");self.j.record_summary_state(rid,state,detail="invalid_result" if state=="rejected" else "disk_full")
            elif state=="stored":self.j.record_summary_result_receipt(typed_result(request),str(uuid4()))
        self.j.close();self.j=TaskRepository(self.root/"tasks.sqlite3")
        observer=SummaryScheduler(repository_factory=lambda:TaskRepository(self.root/"tasks.sqlite3"),adapter=None,
            subject_log=lambda:None,collector=SummarySourceCollector(self.root,lambda:None),
            authority_flags=lambda subject,end:{},paused=lambda:True,priority=lambda:False)
        try:
            for before,request in requests.items():
                expected={"prepared":"not_sent","sending":"unknown"}.get(before,before)
                self.assertEqual(self.j.get_summary_request(request["requestId"])["state"],expected,before)
                replay=deepcopy(request);replay["requestId"]=str(uuid4())
                if expected=="not_sent":self.j.reserve_summary_request(replay)
                else:
                    with self.assertRaises(InvalidTransitionError):self.j.reserve_summary_request(replay)
        finally:observer.close()

    def test_oldest_due_cursor_page_bounded32_coalesces_without_body(self):
        subjects=[{**self.subject,"id":str(uuid4())} for _ in range(40)]
        for n,s in enumerate(subjects):self.j.observe_summary_cursor(s,1,now=n)
        for s in subjects:self.j.observe_summary_cursor(s,2,now=100)
        page=self.j.summary_candidates(500)
        self.assertEqual(len(page),32);self.assertEqual([p["due_at"] for p in page],list(range(300,332)))
        self.assertTrue(all(p["high_water"]==2 for p in page))
        self.assertTrue(all("body" not in p and "text" not in p for p in page))
        with self.assertRaises(InvalidTransitionError):self.j.summary_candidates(500,limit=33)


if __name__=="__main__":unittest.main()
