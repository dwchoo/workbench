"""Opt-in installed SDK + current scheduler/journal/writer, owned fixtures only.

The 14 metadata/compatibility supplier is controlled. The SDK getters and
transport are real; this does not establish operating model output semantics.
Existing09 frozen fixtures are reused read-only. Product8 has separate evidence;
this invocation measures only controlled native-capability composition.
"""
from copy import deepcopy
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from tests.backend.q01_product_adapter_probe.composition import Phase
from tests.backend.q01_product_adapter_probe.runtime import ROOT, HERE
from tests.backend.test_summary_scheduler_independent import CompatibilityPort, AT
from workbench.backend.flow import HandoffJournal
from workbench.backend.flow_terminal import TerminalService, _Command
from workbench.backend.summary_adapter import SDKSummaryAdapter, NATIVE_ESTIMATOR
from workbench.backend.summary_scheduler import SummaryScheduler, SummarySourceCollector
from workbench.backend.paths import ensure_private_dir
from workbench.storage.important_log import ActiveSubjectLog, ImportantLogReader
from workbench.tasks.repository import TaskRepository


class ScriptedProvider(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,runtime):
        self.runtime=runtime;super().__init__(("127.0.0.1",0),ScriptedHandler)


def scripted_summary_params(case):
    params={"facts":["확인한 작업과 검증 결과"],"interpretations":[],"unknowns":[]}
    if case.startswith("contract"):
        # 2001 characters remain valid under the whole-envelope 8KiB cap.
        # An extra raw key exercises the existing exact-key result contract.
        params["facts"]=["x"*2001]
        params["fixtureUnexpected"]=True
    return params


class ScriptedHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        owner=self.server.runtime
        raw=self.rfile.read(int(self.headers.get("Content-Length",0)));body=json.loads(raw)
        encoded=json.dumps(body)
        case=next((name for name,request in owner.requests.items() if request["requestId"] in encoded),None)
        tools=[tool.get("function",{}).get("name") for tool in body.get("tools",[])]
        if case is None or tools!=["db_summary_save"]:
            owner.stop_reason="unattributed_or_expanded_tools";self.send_error(503);return
        row={"case":case,"requestId":owner.requests[case]["requestId"],"monotonic_ns":time.monotonic_ns(),
             "tools":tools,"sha256":sha256(raw).hexdigest(),"body_bytes":len(raw),
             "output_cap":body.get("max_completion_tokens"),"tool_result_messages":sum(m.get("role")=="tool" for m in body.get("messages",[]))}
        with owner.lock:
            row["body_path"]=str(owner.private/f"post-{len(owner.posts)+1:03}.json");owner.posts.append(row)
        Path(row["body_path"]).write_bytes(raw)
        if owner.held_case==case and not owner.held.is_set():
            owner.held.set();owner.release.wait(4)
        count=sum(m.get("role")=="tool" for m in body.get("messages",[]))
        if count==0:
            params=scripted_summary_params(case)
            calls=[{"index":0,"id":case+"-save","type":"function","function":{"name":"db_summary_save","arguments":json.dumps(params)}}]
            if case=="contract_then_bash":
                calls.append({"index":1,"id":case+"-bash","type":"function","function":{"name":"bash","arguments":json.dumps({"command":"echo CHANGED > "+str(owner.private/"project/sentinel")})}})
            delta={"role":"assistant","tool_calls":calls};finish="tool_calls"
        else:delta={"role":"assistant","content":"done"};finish="stop"
        payload={"id":"dvl10-owned","object":"chat.completion.chunk","created":0,"model":"scripted",
            "choices":[{"index":0,"delta":delta,"finish_reason":finish}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}
        data=("data: "+json.dumps(payload)+"\n\ndata: [DONE]\n\n").encode()
        self.send_response(200);self.send_header("Content-Type","text/event-stream");self.send_header("Content-Length",str(len(data)));self.end_headers()
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass


def build_test_extension(path):
    # Only a private test control adapter changes: same public installed SDK and
    # product dispatch. Existing harness source and canonical product are read-only.
    original=(HERE/"composition_extension.ts").read_text()
    original=original.replace('"../../../omp_bridge/g3/summary_sdk.ts"',json.dumps(str(ROOT/"omp_bridge/g3/summary_sdk.ts")))
    original=original.replace('import {dispatchSummarySDK}', 'import {dispatchSummarySDK,countSummaryFragments}')
    original=original.replace('maxTokens:parent.model.maxTokens}',
                              'maxTokens:parent.model.maxTokens,tokenizer:parent.model.tokenizer}')
    ready=' writeFileSync(dir+"/metadata.json"'
    assert original.count(ready)==1
    original=original.replace(ready, ''' if(parent.model.tokenizer!=="claude-v3"||ctx.model?.tokenizer!==parent.model.tokenizer)
  throw Error("controlled_parent_tokenizer_unavailable");
 let countIndex=0;
 const serveCount=()=>{
  const queryPath=dir+"/count-query-"+countIndex+".json";
  if(!existsSync(queryPath))return;
  const query=JSON.parse(readFileSync(queryPath,"utf8"));
  const proof=countSummaryFragments(metadata(),query.fragments,{Tokenizer});
  log({event:"actual_native_count",index:countIndex,ownerDigest:proof.ownerDigest,
   fragmentDigests:proof.fragmentDigests,tokens:proof.tokens,encoding:proof.encoding,
   exact:proof.exact,estimator:proof.estimator,fragmentBytes:query.fragments.map((f:string)=>Buffer.byteLength(f))});
  writeFileSync(dir+"/count-reply-"+countIndex+".json.tmp",JSON.stringify(proof));
  renameSync(dir+"/count-reply-"+countIndex+".json.tmp",dir+"/count-reply-"+countIndex+".json");
  countIndex++;
 };
'''+ready)
    original=original.replace('readFileSync,writeFileSync}', 'readFileSync,writeFileSync,renameSync}')
    start=original.index(' const deadline=Date.now()+30000;')
    stop=original.index('  let creates=0;',start)
    replacement=''' for(let index=0;index<6;index++){
  const job=dir+"/job-"+index+".json";
  const limit=Date.now()+45000;
  while(!existsSync(job)&&Date.now()<limit){serveCount();await new Promise(r=>setTimeout(r,10));}
  if(!existsSync(job))throw Error("owned_scheduler_job_timeout");
  const {name,value}=JSON.parse(readFileSync(job,"utf8"));
'''
    original=original[:start]+replacement+original[stop:]
    path.write_text(original)
    return {"frozen_fixture_sha256":sha256((HERE/"composition_extension.ts").read_bytes()).hexdigest(),
            "test_extension_sha256":sha256(path.read_bytes()).hexdigest(),
            "differences":"absolute canonical product import; actual parent tokenizer readback; count-only owner files in same installed SDK process; bounded sequential owner jobs; no product guard/SDK/frozen fixture modified"}


def start_native_phase(phase):
    """Declare the controlled family through normal models.yml before OMP launch."""
    launch=subprocess.Popen
    config=phase.private/"agent/models.yml"
    def configured_launch(argv,*args,**kwargs):
        assert argv[argv.index("--model")+1]=="currentproduct/scripted"
        assert Path(kwargs["cwd"])==phase.private/"project"
        original=config.read_text()
        assert original.count("        reasoning: true\n")==1 and "tokenizer:" not in original
        configured=original.replace("        reasoning: true\n",
                                    "        reasoning: true\n        tokenizer: claude-v3\n")
        config.write_text(configured)
        phase.native_configuration={"path":str(config),"sha256":sha256(configured.encode()).hexdigest(),
            "family":"claude-v3","source":"owned models.yml through normal OMP model resolution; no parent object mutation"}
        return launch(argv,*args,**kwargs)
    # This narrow launch boundary ends before scheduler or SDK children run.
    with patch("tests.backend.q01_product_adapter_probe.runtime.subprocess.Popen",configured_launch):
        phase.start()
    assert phase.metadata["model"].get("tokenizer")=="claude-v3"


class OwnerCountPort:
    """Fixture-only count exchange; it does not reproduce the G3 count frame."""
    def __init__(self,phase):self.phase=phase;self.rows=[]
    def __call__(self,query):
        index=len(self.rows)
        request=self.phase.private/f"count-query-{index}.json"
        temporary=request.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(query,ensure_ascii=False))
        temporary.replace(request)
        reply=self.phase.private/f"count-reply-{index}.json"
        until=time.monotonic()+5
        while not reply.exists():
            if self.phase.stop_reason or any(e.get("event")=="fatal" for e in self.phase.events()):
                raise RuntimeError("owned_native_count_failed")
            if time.monotonic()>until:raise TimeoutError("owned_native_count_timeout")
            time.sleep(.01)
        proof=json.loads(reply.read_text())
        self.rows.append({"index":index,"query_sha256":sha256(request.read_bytes()).hexdigest(),
                          "proof":proof})
        if proof.get("exact") is not True or proof.get("estimator")!=NATIVE_ESTIMATOR:
            raise RuntimeError("controlled_native_count_unavailable")
        return proof


class SchedulerTransport:
    """Existing adapter's request seam; owner files are test transport, not product queue."""
    def __init__(self,phase):self.phase=phase;self.case=None;self.count=0;self.frames=[]
    def request(self,role,frame,*,timeout,expected_peer,authority_token):
        assert role=="manager" and frame["kind"]=="summary_request"
        assert authority_token.current() is True and authority_token.claim() is True
        name=self.case;assert name is not None
        prepared=deepcopy(frame["summary"]);self.phase.requests[name]=deepcopy(prepared["request"])
        self.frames.append({"case":name,"requestId":prepared["request"]["requestId"],"expected_peer":expected_peer,"at_ns":time.monotonic_ns()})
        (self.phase.private/"admission.json").write_text(json.dumps({name:True}))
        (self.phase.private/f"job-{self.count}.json").write_text(json.dumps({"name":name,"value":prepared},ensure_ascii=False))
        self.count+=1
        result=self.phase.result(name)
        self.phase.advance(name)
        return result


def collect_scenario_observation(name,status,control,requests,posts,read_journal,read_sdk):
    """Keep a pre-dispatch refusal distinct from an observed SDK outcome."""
    outcome={"status":status,"control":control}
    if name not in requests:
        case_posts=[row for row in posts if row.get("case")==name]
        if (status.get("state") in {"deferred","not_sent"}
                and status.get("requestId") is None and status.get("reason")
                and not case_posts):
            outcome.update(observation="predispatch_deferred",provider_posts=0)
            return outcome
        raise AssertionError(f"missing_reserved_request:{name}")
    request=requests[name]
    outcome["journal"]=read_journal(request["requestId"])
    outcome["sdk"]=read_sdk(name)
    return outcome


def mark_dependent_scenarios_not_run(observed,cases,index):
    name=cases[index]
    observed.update(passed=False,blocked_case=name,incomplete_reason="predispatch_deferred")
    for dependent in cases[index+1:]:
        observed["outcomes"][dependent]={"observation":"not_run",
            "reason":"dependent_prior_predispatch_defer","depends_on":name}


def scheduler_composition(output):
    output.mkdir(parents=True,exist_ok=False,mode=0o700)
    extension=output/"owner-scheduler-extension.ts"
    extension_custody=build_test_extension(extension)
    phase=Phase(output/"owned-omp");phase.extension=extension
    scheduler=None;events=None;count_port=OwnerCountPort(phase)
    observed={"observation_boundary":"Controlled fixture native capability composition: actual product scheduler/journal/adapter/SDK child/writer, same-process actual SDK strict count. runtime.count_fragments owner-file fixture bypasses adapter.count_fragments/G3 count frame; not B IPC, operating14, provider-token accuracy or current8 evidence."}
    try:
        with patch("tests.backend.q01_product_adapter_probe.runtime.Provider",ScriptedProvider):start_native_phase(phase)
        root=phase.private/"data";ensure_private_dir(root/"workflow/terminal")
        events=HandoffJournal(root/"workflow/handoffs.jsonl")
        sink=ActiveSubjectLog(phase.catalog);factory=lambda:TaskRepository(root/"tasks.sqlite3")
        with factory() as j:
            os.chmod(root/"tasks.sqlite3",0o600)
            task=j.create_task({"goal":"독립 scheduler SDK 작업 요약"});j.approve_scope(task,1,{"paths":["owned fixture"]});j.proceed(task,1,"approved private fixture")
        delegated={"seq":500,"at":AT,"type":"delegated","task_id":task,"revision":1,"scope_decision_id":str(uuid4()),"proceed_decision_id":str(uuid4())}
        flow_path=root/"workflow/tasks-flow.jsonl";flow_path.write_text(json.dumps(delegated)+"\n");os.chmod(flow_path,0o600)
        sink.record_flow(delegated)
        command_id=str(uuid4());logpath=root/"workflow/terminal"/(command_id+".log");logpath.touch(mode=0o600)
        start=events.append({"type":"terminal_started","command_id":command_id,"task_id":task,"started_at":AT,"command":"owned fixture work"})
        sink.record_terminal(start,revision=1)
        producer=TerminalService.__new__(TerminalService);producer._repository_factory=factory;producer._subject_log=sink
        producer._mark_subject_unknown=lambda:(_ for _ in ()).throw(AssertionError("producer capture failed"))
        command=_Command(command_id,"private fixture",{},task,logpath);command.summary_started=True;command.summary_revision=1
        producer._configure_summary_capture(command)
        clock=[0.];paused=[False];priority=[False];compat=CompatibilityPort();transport=SchedulerTransport(phase)
        runtime={"metadata":deepcopy(phase.metadata),"instructions":phase.definition,"expected_peer":(str(uuid4()),1),
            "count_fragments":count_port,
            "writer":{"python":sys.executable,"moduleRoot":str(ROOT/"src"),"dataRoot":str(root)}}
        scheduler=SummaryScheduler(repository_factory=factory,adapter=SDKSummaryAdapter(transport),subject_log=lambda:sink,
            collector=SummarySourceCollector(root,lambda:sink),authority_flags=lambda subject,end:{"active":subject["id"]==task,"revoked":False},
            paused=lambda:paused[0],priority=lambda:priority[0],runtime=lambda:runtime,storage_ready=lambda:True,compatibility=compat,clock=lambda:clock[0])
        def settle():
            until=time.monotonic()+18
            while scheduler.status().get("busy") or scheduler.status()["inFlight"]:
                if phase.stop_reason:raise AssertionError(phase.stop_reason)
                if time.monotonic()>until:raise TimeoutError(str(scheduler.status()))
                time.sleep(.01)
            return scheduler.status()
        def tick(t):clock[0]=t;scheduler.tick();return settle()
        def control():
            with factory() as j:return j.summary_control_state()
        command.write(b"first task-bound work\n");tick(0)
        tick(299);assert transport.count==0 and not phase.posts
        paused[0]=True;tick(300);assert transport.count==0 and not phase.posts
        paused[0]=False;priority[0]=True;tick(300);assert transport.count==0 and not phase.posts
        priority[0]=False
        observed["before_dispatch"]={"at299_posts":0,"paused_posts":0,"priority_posts":0,"task":task,"command":command_id,"revision":1}
        cases=("task_positive","contract1","contract2","contract3","recovery_ok","contract_then_bash")
        outcomes={}
        observed["outcomes"]=outcomes # Preserve partial actual outcomes on first failed assertion.
        for index,name in enumerate(cases):
            if index:
                command.write((name+" never-sent work\n").encode())
                tick(clock[0]) # discover fresh dirty high water without a forced end
                due=clock[0]+300
            else:due=300
            if name=="recovery_ok":
                protected=control();assert protected["protection"]=="summary_contract_failed"
                with factory() as j:unresolved=[r["request"]["requestId"] for r in j.list_unresolved_summary_requests()]
                observed["protected_unresolved"]=unresolved
                attempt=scheduler.recover("explicit-private-scripted-verify")
                assert attempt["plumbing_verified"] and attempt["real_model_contract"]=="unverified"
                observed["recovery_attempt"]=attempt
            transport.case=name
            status=tick(due)
            with factory() as j:
                outcomes[name]=collect_scenario_observation(name,status,control(),phase.requests,phase.posts,
                    j.get_summary_request,lambda case:json.loads((phase.private/f"result-{case}.json").read_text()))
            if outcomes[name].get("observation")=="predispatch_deferred":
                mark_dependent_scenarios_not_run(observed,cases,index)
                observed.update(frames=transport.frames,control_incidents=compat.incidents,
                    producer={"observedBytes":command.summary_observed,"rawBytes":len(logpath.read_bytes()),
                        "rawSha256":sha256(logpath.read_bytes()).hexdigest(),"taskBoundRevision":command.summary_revision,"summaryFailed":command.summary_failed})
                return observed
            if name=="task_positive":assert outcomes[name]["journal"]["state"]=="stored"
            elif name.startswith("contract") and name!="contract_then_bash":
                assert outcomes[name]["sdk"]["reason"]=="invalid_result",outcomes[name]
                assert outcomes[name]["control"]["streak"]==index,outcomes[name]
                assert (outcomes[name]["control"]["protection"] is None)==(index<3),outcomes[name]
            elif name=="recovery_ok":
                assert outcomes[name]["journal"]["state"]=="stored"
                assert outcomes[name]["control"]["streak"]==0 and outcomes[name]["control"]["recovery_attempt"] is None
            else:
                assert outcomes[name]["sdk"]["reason"]=="guard_violation_observed",outcomes[name]
                assert outcomes[name]["control"]["protection"]=="guard_violation_observed",outcomes[name]
        assert (phase.private/"project/sentinel").read_text()=="ORIGINAL"
        with factory() as j:
            assert j.pending_summary_sources({"schemaVersion":1,"kind":"task","id":task},revision=1)==[]
        observed.update(outcomes=outcomes,frames=transport.frames,control_incidents=compat.incidents,
            producer={"observedBytes":command.summary_observed,"rawBytes":len(logpath.read_bytes()),"rawSha256":sha256(logpath.read_bytes()).hexdigest(),"taskBoundRevision":command.summary_revision,"summaryFailed":command.summary_failed})
        observed["runtime"]=phase.finish()
        observed["passed"]=True
    except Exception as error:
        observed.update(passed=False,error=f"{type(error).__name__}: {error}",events=phase.events(),posts=phase.posts)
        raise
    finally:
        if scheduler is not None:observed["scheduler_cleanup"]=scheduler.close()
        if events is not None:events.close()
        phase.release.set();phase.close()
        if phase.process is not None:observed["runtime_final"]=phase.snapshot()
        observed["extension_custody"]=extension_custody
        observed["native_configuration"]=getattr(phase,"native_configuration",None)
        observed["actual_parent_metadata"]=getattr(phase,"metadata",None)
        observed["native_count_queries"]=count_port.rows
        (output/"result.json").write_text(json.dumps(observed,ensure_ascii=False,indent=2))
    return observed


def assert_budget_omissions_pending(output):
    """Inspect a stopped runtime's journal read-only; never replay its request."""
    output=Path(output).resolve()
    assert output.is_relative_to(ROOT/".workflow/dvl-local")
    private=output/"owned-omp/private"
    prepared=json.loads((private/"job-0.json").read_text())["value"]
    omitted=[item["range"] for item in prepared["omitted"] if item["reason"]=="input_budget"]
    assert len(omitted)==2 and prepared["request"]["sourceGaps"]==[]
    database=private/"data/tasks.sqlite3"
    assert not Path(str(database)+"-wal").exists(), "closed journal required for immutable inspection"
    before=sha256(database.read_bytes()).hexdigest()
    connection=sqlite3.connect(database.as_uri()+"?mode=ro&immutable=1",uri=True)
    connection.row_factory=sqlite3.Row
    repository=TaskRepository.__new__(TaskRepository)
    repository._connection=connection
    repository._closed=False
    try:
        request=prepared["request"]
        pending=repository.pending_summary_sources(request["subject"],revision=request["revision"])
        assert sorted(json.dumps(row["range"],sort_keys=True) for row in pending)==sorted(json.dumps(span,sort_keys=True) for span in omitted)
        candidates=repository.summary_candidates(600)
        owner=[item for item in candidates if item["subject"]==request["subject"]]
        assert len(owner)==1 and owner[0]["due_at"]==600
        assert repository.get_summary_request(request["requestId"])["state"]=="rejected"
    finally:
        connection.close()
    after=sha256(database.read_bytes()).hexdigest()
    assert before==after
    return {"passed":True,"omitted_input_budget":omitted,"pending":pending,"next_period_candidate":owner[0],
        "source_gaps":[],"rejected_sent_range_replayed":False,"inspection":"actual persisted product journal getters on immutable read-only connection; no runtime rerun",
        "database_sha256_before":before,"database_sha256_after":after}


def main():
    if os.environ.get("WORKBENCH_RUN_PRODUCT_SDK")!="1":raise RuntimeError("explicit owned SDK opt-in required")
    out=Path(sys.argv[1]).resolve()
    assert out.is_relative_to(ROOT/".workflow/dvl-local") and not out.exists()
    out.mkdir(mode=0o700)
    identity={"source_sha256":{name:sha256((ROOT/name).read_bytes()).hexdigest() for name in
        ("omp_bridge/g3/summary_sdk.ts","src/workbench/backend/summary_adapter.py","src/workbench/backend/summary_scheduler.py",
         "src/workbench/tasks/repository.py","tests/backend/q01_product_adapter_probe/runtime.py",
         "tests/backend/q01_product_adapter_probe/composition_extension.ts","tools/q01_recheck.py",
         "src/workbench/contracts/summary_compat_policy_v1.json")},
        "omp_binary_path":str(Path(shutil.which("omp")).resolve()),
        "omp_binary_sha256":sha256(Path(shutil.which("omp")).resolve().read_bytes()).hexdigest(),
        "root_uid":out.stat().st_uid,"root_mode":oct(out.stat().st_mode&0o777),
        "metadata_supplier":"controlled14 fixture, not operating model metadata/output semantics",
        "inputs_and_caps":"existing scheduler fixture and8192/10%/32768 unchanged"}
    assert identity["root_uid"]==os.getuid() and identity["root_mode"]=="0o700"
    (out/"candidate-identity.json").write_text(json.dumps(identity,indent=2))
    actual=scheduler_composition(out/"scheduler-sdk")
    result={"passed":actual["passed"],"actual_scheduler":str(out/"scheduler-sdk/result.json"),
        "fresh_product8":None,"fresh_product8_status":"separate_existing_evidence_not_rerun_or_combined",
        "evidence_label":"controlled fixture native capability composition",
        "metadata_supplier":"controlled family via normal owned model config; operating metadata/output semantics remain unknown",
        "remaining_owned":[] if actual["runtime_final"]["pgid_absent"] else [actual["runtime_final"]["pid"]]}
    (out/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
    if not result["passed"]:raise SystemExit(2)


if __name__=="__main__":main()
