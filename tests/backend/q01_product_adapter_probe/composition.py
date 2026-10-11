"""Requirement-derived product runtime compositions. No old probe verdicts."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4
from .runtime import ProductRuntime, HERE, _request
from workbench.backend.summary_adapter import SourcePiece, prepare_summary, skill_bytes
from workbench.contracts.logs_v1 import parse_summary_request, summary_input_key
from workbench.storage.important_log import ImportantLogCatalog, SubjectLifecycle
from workbench.tasks.repository import TaskRepository, InvalidTransitionError

STAMP="2026-10-12T00:00:00Z"


def wait(runtime, predicate, limit=8):
    deadline=time.monotonic()+limit
    while time.monotonic()<deadline:
        if runtime.stop_reason or any(e.get("event")=="fatal" for e in runtime.events()):
            raise RuntimeError(runtime.stop_reason or str(runtime.events()[-1]))
        if predicate(): return
        time.sleep(.01)
    raise TimeoutError("bounded composition wait")


class Phase(ProductRuntime):
    def __init__(self, output):
        super().__init__(output)
        self.extension=HERE/"composition_extension.ts"
        self.no_send=set()
        self.held_case=None
        self.held=threading.Event();self.release=threading.Event()
        self.extra_env={"Q01_PRODUCT_STEP":"1"}

    def start(self):
        self.output.mkdir(parents=True,mode=0o700,exist_ok=True)
        for name in ("home","project","data","socket-dir"):
            (self.private/name).mkdir(parents=True,exist_ok=True,mode=0o700)
        self.prepare_route({})
        self.metadata=json.loads((self.private/"metadata.json").read_text())
        self.definition=(self.private/"definition.txt").read_text()
        self.catalog=ImportantLogCatalog(self.private/"data/important-logs",create=True)

    def prepare(self, name, request, text):
        request={**request,"skillName":"important-log-summary","dbHealth":self.catalog.health_snapshot()["active"]}
        self.prepared[name]=prepare_summary(request,[SourcePiece(request["sources"][0],text,"start_spec")],
                                            metadata=self.metadata,instructions=self.definition)
        self.requests[name]=deepcopy(self.prepared[name]["request"])
        return self.requests[name]

    def launch(self, admissions):
        (self.private/"admission.json").write_text(json.dumps(admissions))
        (self.private/"prepared.json").write_text(json.dumps(self.prepared,ensure_ascii=False))

    def result(self,name):
        wait(self,lambda:(self.private/f"result-{name}.json").exists())
        return json.loads((self.private/f"result-{name}.json").read_text())

    def advance(self,name): (self.private/f"advance-{name}").write_text("owner-outcome-recorded")

    def finish(self):
        wait(self,lambda:any(e.get("event")=="done" for e in self.events()))
        self.close()
        return self.snapshot()

    def snapshot(self):
        absent=False
        try:os.killpg(self.process.pid,0)
        except ProcessLookupError:absent=True
        results={p.stem[7:]:json.loads(p.read_text())for p in self.private.glob("result-*.json")}
        report={"route":self.route,"posts":self.posts,"events":self.events(),"results":results,
                "stop_reason":self.stop_reason,"pid":self.process.pid,"pgid_absent":absent,
                "observation_boundary":"Actual installed SDK product dispatch/writer, test-owner admission and journal disposition; Automation wiring belongs to DVL10"}
        (self.output/"runtime-evidence.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
        return report


def reserve(repo, request):
    repo.reserve_summary_request(request);repo.record_summary_state(request["requestId"],"sending")


def origin(out):
    r=Phase(out);details={};decisions={};requests={}
    try:
        r.start();life=SubjectLifecycle(r.private/"data/important-logs",r.catalog.health_snapshot()["active"]["dbUuid"])
        with TaskRepository(r.private/"data/tasks.sqlite3")as repo:
            os.chmod(r.private/"data/tasks.sqlite3",0o600)
            base,_=_request(repo,"작업 변경과 검증 범위",sha256(skill_bytes()).hexdigest())
            text="승인된 작업의 변경 내용과 검증 결과";raw=text.encode();task=base["subject"]["id"];command=str(uuid4());grant=base["authorizationRef"]
            life.record_flow({"seq":1,"at":STAMP,"type":"delegated","task_id":task,"revision":1,
                              "scope_decision_id":str(uuid4()),"proceed_decision_id":grant})
            life.record_terminal({"seq":2,"at":STAMP,"type":"terminal_started","command_id":command,"task_id":task,"started_at":STAMP},revision=1)
            source={"schemaVersion":1,"sourceKind":"terminal","collectorEpoch":1,"taskId":task,"revision":1,"commandId":command}
            span={"schemaVersion":1,"source":source,"start":0,"end":len(raw),"digest":sha256(raw).hexdigest()}
            life.record_terminal_range(base["subject"],str(uuid4()),span,observed_at=STAMP)
            auth=life.authority(base["subject"],active=True,paused=False,revoked=False,has_new_input=True)
            taskreq=parse_summary_request({**base,"requestId":str(uuid4()),"authorizationRef":auth["authorization_ref"],
              "authorizationEpoch":auth["authorization_epoch"],"sources":[span],"sourceGaps":[],"inputKey":summary_input_key(base["subject"],1,[span],[],base["skillDigest"])})
            decisions["task_grant"]=life.authorize_summary_request(taskreq,active=True,paused=False,revoked=False,has_new_input=True)
            requests["task_grant"]=r.prepare("task_grant",taskreq,text);reserve(repo,requests["task_grant"])
            for name,change,flags in (("unrelated",{"authorizationRef":str(uuid4())},{}),("paused",{}, {"paused":True}),("revoked",{}, {"revoked":True})):
                req={**taskreq,**change,"requestId":str(uuid4())};requests[name]=r.prepare(name,req,text)
                decisions[name]=life.authorize_summary_request(req,active=True,paused=flags.get("paused",False),revoked=flags.get("revoked",False),has_new_input=True)
            cmd=str(uuid4());subject={"schemaVersion":1,"kind":"command","id":cmd};ctext="직접 실행한 작업 내용과 종료 결과";craw=ctext.encode()
            life.record_terminal({"seq":3,"at":STAMP,"type":"terminal_started","command_id":cmd,"task_id":None,"started_at":STAMP})
            cs={"schemaVersion":1,"sourceKind":"terminal","collectorEpoch":1,"commandId":cmd};sp={"schemaVersion":1,"source":cs,"start":0,"end":len(craw),"digest":sha256(craw).hexdigest()}
            life.record_terminal_range(subject,str(uuid4()),sp,observed_at=STAMP)
            life.record_terminal({"seq":4,"at":STAMP,"type":"terminal_ended","command_id":cmd,"status":"exited","ended_at":STAMP})
            ca=life.authority(subject,active=False,paused=False,revoked=False,has_new_input=True,closing_event=True)
            creq={**taskreq,"requestId":str(uuid4()),"subject":subject,"revision":None,"authorizationRef":ca["authorization_ref"],"authorizationEpoch":ca["authorization_epoch"],"sources":[sp],"inputKey":summary_input_key(subject,None,[sp],[],base["skillDigest"])}
            requests["terminal_close"]=r.prepare("terminal_close",creq,ctext);reserve(repo,requests["terminal_close"])
            decisions["terminal_close"]=life.authorize_summary_request(creq,active=False,paused=False,revoked=False,has_new_input=True,closing_event=True)
            for name in ("notice_only","user_role_only"):
                unknown={"schemaVersion":1,"kind":"command","id":str(uuid4())}
                decisions[name]=life.authority(unknown,active=True,paused=False,revoked=False,has_new_input=True)
                # A notice/role alone grants no authority: even valid work input cannot be dispatched.
                requests[name]=r.prepare(name,{**creq,"requestId":str(uuid4())},ctext)
        r.no_send={name for name,d in decisions.items()if not d["allowed"]};r.launch({n:d["allowed"]for n,d in decisions.items()})
        for name in r.prepared:
            details[name]=r.result(name);r.advance(name)
        report=r.finish()
        life.mark_command_end_summary_processed(cmd)
        again=life.authority(subject,active=False,paused=False,revoked=False,has_new_input=True,closing_event=True)
        with TaskRepository(r.private/"data/tasks.sqlite3")as repo:
            try:repo.reserve_summary_request({**requests["terminal_close"],"requestId":str(uuid4())})
            except InvalidTransitionError: replay=False
            else:replay=True
        checks={"task_grant_bound":decisions["task_grant"]["allowed"] and details["task_grant"]["status"]=="summary_result",
                "command_start_bound":decisions["terminal_close"]["allowed"],"terminal_close_bound":details["terminal_close"]["status"]=="summary_result",
                "work_source_ranges_bound":all(details[n].get("result",{}).get("sources")==requests[n]["sources"] for n in ("task_grant","terminal_close")),
                "unrelated_scope_rejected":not decisions["unrelated"]["allowed"] and details["unrelated"].get("sent")is False,
                "notice_not_authority":not decisions["notice_only"]["allowed"] and details["notice_only"].get("sent")is False,
                "user_role_not_authority":not decisions["user_role_only"]["allowed"] and details["user_role_only"].get("sent")is False,
                "pause_revoke_preserved":all(not decisions[n]["allowed"]and details[n].get("sent")is False for n in ("paused","revoked")),
                "end_once_no_replay":not again["allowed"]and not replay}
        report.update(decisions=decisions,end_again=again,checks=checks,source_fixture="Synthetic trusted terminal/Task lifecycle facts via actual SubjectLifecycle; not user-utterance audit")
        (out/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2));return report
    finally:r.close()


def disconnect(out):
    r=Phase(out)
    try:
        r.start()
        with TaskRepository(r.private/"data/tasks.sqlite3")as repo:
            os.chmod(r.private/"data/tasks.sqlite3",0o600)
            base,_=_request(repo,"전송된 미확정 작업 범위",sha256(skill_bytes()).hexdigest())
            r.prepare("killed",base,"전송된 미확정 작업 범위");reserve(repo,r.requests["killed"])
        r.held_case="killed";r.launch({"killed":True})
        if not r.held.wait(8):raise TimeoutError("kill POST not held")
        r.stop_process();r.release.set();r.close()
        before=r.snapshot()
        with TaskRepository(r.private/"data/tasks.sqlite3")as repo:
            repo.record_summary_state(base["requestId"],"unknown",detail="owned OMP killed after actual POST")
        # Relaunch same private configuration and durable journal; retain original trace first.
        for name in ("trace.jsonl","metadata.json","prepared.json"):
            (r.private/name).rename(r.private/("pre-restart-"+name))
        for artifact in (*r.private.glob("post-*.json"), r.output/"omp-stdout.log", r.output/"omp-stderr.log"):
            renamed=artifact.with_name("pre-restart-"+artifact.name)
            artifact.rename(renamed)
            for post in before["posts"]:
                if post.get("body_path")==str(artifact): post["body_path"]=str(renamed)
        r.process=None;r.provider=None;r.thread=None;r.streams=[];r.posts=[];r.prepared={};r.requests={};r.held_case=None;r.held.clear();r.release.clear();r.closed=False
        r.start()
        with TaskRepository(r.private/"data/tasks.sqlite3")as repo:
            unresolved=repo.list_unresolved_summary_requests()
            try:repo.reserve_summary_request({**base,"requestId":str(uuid4())})
            except InvalidTransitionError:denied=True
            else:denied=False
            text="재시작 뒤 신규 작업 범위";new=deepcopy(base);new["requestId"]=str(uuid4());oldend=new["sources"][0]["end"];new["sources"][0].update(start=oldend,end=oldend+len(text.encode()),digest=sha256(text.encode()).hexdigest());new["inputKey"]=summary_input_key(new["subject"],new["revision"],new["sources"],[],new["skillDigest"])
            r.prepare("new_range",new,text);reserve(repo,r.requests["new_range"])
        r.launch({"new_range":True});result=r.result("new_range");r.advance("new_range");after=r.finish()
        check=denied and any(v["request"]["requestId"]==base["requestId"]for v in unresolved)and result["status"]=="summary_result" and not any(p["case"]=="killed"for p in after["posts"])
        report={"before":before,"after":after,"overlap_denied":denied,"unresolved":unresolved,"new_range":result,"checks":{"disconnect_reconciled":check,"unknown_not_replayed":check},"omp_killed_late":"N/A: child killed with same OMP process"}
        (out/"disconnect.json").write_text(json.dumps(report,ensure_ascii=False,indent=2));return report
    finally:r.release.set();r.close()


def recovery(out):
    r=Phase(out);outcomes={};states={}
    try:
        r.start()
        with TaskRepository(r.private/"data/tasks.sqlite3")as repo:
            os.chmod(r.private/"data/tasks.sqlite3",0o600)
            base,_=_request(repo,"전송 전 보류된 작업 범위",sha256(skill_bytes()).hexdigest())
            r.prepare("paused",base,"전송 전 보류된 작업 범위")
            r.prepare("resume",{**base,"requestId":str(uuid4())},"전송 전 보류된 작업 범위")
            late,_=_request(repo,"늦게 도착하는 실제 결과",sha256(skill_bytes()).hexdigest());r.prepare("late",late,"늦게 도착하는 실제 결과")
            reserve(repo,r.requests["paused"]);reserve(repo,r.requests["late"])
        r.held_case="late"
        # Missing/corrupt persisted predicate fails closed. Driver reads, does not silently default open.
        (r.private/"admission.json").write_text("invalid-json")
        (r.private/"prepared.json").write_text(json.dumps(r.prepared,ensure_ascii=False))
        outcomes["paused"]=r.result("paused")
        with TaskRepository(r.private/"data/tasks.sqlite3")as repo:
            repo.record_summary_state(base["requestId"],"not_sent",detail="paused fail-closed owner disposition")
            reserve(repo,r.requests["resume"])
            states["not_sent"]=repo.get_summary_request(base["requestId"])
        (r.private/"admission.json").write_text(json.dumps({"resume":True,"late":True}));r.advance("paused")
        outcomes["resume"]=r.result("resume");r.advance("resume")
        if not r.held.wait(8):raise TimeoutError("actual late POST not held")
        with TaskRepository(r.private/"data/tasks.sqlite3")as repo:repo.record_summary_state(late["requestId"],"unknown",detail="host timeout; owned SDK alive")
        with TaskRepository(r.private/"data/tasks.sqlite3")as reopened:
            states["restart_unresolved"]=reopened.list_unresolved_summary_requests()
        r.release.set();outcomes["late"]=r.result("late");r.advance("late");report=r.finish()
        with TaskRepository(r.private/"data/tasks.sqlite3")as repo:
            states["late_final"]=repo.get_summary_request(late["requestId"])
            duplicate=repo.record_summary_result_receipt(outcomes["late"]["result"],str(uuid4()))is False
            try:repo.reserve_summary_request({**r.requests["late"],"requestId":str(uuid4())})
            except InvalidTransitionError:overlap_refused=True
            else:overlap_refused=False
        checks={"pause_blocks_new":outcomes["paused"].get("sent")is False and not any(p["case"]=="paused"for p in report["posts"]),
                "restart_reconciled":any(x["request"]["requestId"]==late["requestId"] for x in states["restart_unresolved"]),
                "late_result_bound":outcomes["late"]["status"]=="summary_result" and states["late_final"]["state"]=="stored" and duplicate,
                "unknown_not_replayed":overlap_refused,"disconnect_reconciled":None}
        kill=disconnect(out/"disconnect");checks["disconnect_reconciled"]=kill["checks"]["disconnect_reconciled"];checks["unknown_not_replayed"]=checks["unknown_not_replayed"]and kill["checks"]["unknown_not_replayed"]
        report.update(disconnect=kill,outcomes=outcomes,states=states,checks=checks,omp_killed_late="N/A: SDK child is in the same owned OMP process")
        (out/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2));return report
    finally:r.release.set();r.close()
