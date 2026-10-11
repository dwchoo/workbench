"""Two private TUI product-child overlap; frozen G3 protocol facilities only."""
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import select
import stat
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from uuid import uuid4
from .composition import Phase, reserve, wait
from .runtime import _request, skill_bytes, ROOT
from tests.backend.q01_sdk_budget_probe.run_priority_runtime import RecordingBridge
from workbench.observation.worker_review import ActiveRunRef, SerializedReviewAdmission, WorkerReviewScheduler
from workbench.tasks.repository import TaskRepository


def launch_tui(argv, private, env, logfile):
    master,slave=os.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack("HHHH",30,100,0,0))
    actual_env={**env,"TERM":"xterm-256color","COLORTERM":"truecolor"}
    try:p=subprocess.Popen([shutil.which("setsid"),"--ctty",*argv],cwd=private/"project",env=actual_env,stdin=slave,stdout=slave,stderr=slave,close_fds=True)
    finally:os.close(slave)
    os.set_blocking(master,False);stop=threading.Event()
    def drain():
        with logfile.open("wb")as output:
            while not stop.is_set():
                try:
                    if select.select([master],[],[],.1)[0]:
                        raw=os.read(master,65536)
                        if not raw:return
                        output.write(raw)
                except OSError:return
    thread=threading.Thread(target=drain,daemon=True);thread.start()
    return p,master,thread,stop


def run(out):
    out.mkdir(parents=True,mode=0o700)
    socket_dir=ROOT/".workflow/dvl-local"/("q3-"+uuid4().hex[:8]);socket_dir.mkdir(mode=0o700)
    socket_path=socket_dir/"bridge.sock"
    owner_stat=socket_dir.stat();socket_bytes=len(os.fsencode(str(socket_path)))
    assert socket_path.is_absolute() and socket_bytes<=107, "owned fixture socket path exceeds Linux limit"
    assert owner_stat.st_uid==os.getuid() and stat.S_IMODE(owner_stat.st_mode)==0o700, "unsafe fixture socket directory"
    socket_boundary={"path":str(socket_path),"path_utf8_bytes":socket_bytes,"max_path_bytes":107,"uid":owner_stat.st_uid,"mode":oct(stat.S_IMODE(owner_stat.st_mode)),"delivery":"fixture WORKBENCH_G3_BRIDGE_SOCKET environment to actual bridge; product launcher path generation untested"}
    bridge=RecordingBridge(socket_path,{r:str(uuid4())for r in ("manager","worker")},out/"wire.jsonl")
    os.chmod(socket_path,0o600)
    server=threading.Thread(target=bridge.serve_forever,daemon=True);server.start()
    phases={};records=[];states=[];data={"clock":"time.monotonic_ns","boundary":"Actual product SDK dispatch/writer + native bridge notices, actual scheduler with injected due clock; TaskMailbox/scheduling composition belongs to DVL10. Mutex spans unavailable."}
    data["socket_boundary"]=socket_boundary
    def request(role,frame):
        sent=time.monotonic_ns();ack=bridge.request(role,frame,timeout=2);at=time.monotonic_ns()
        records.append({"role":role,"frame":frame,"sent_ns":sent,"ack":ack,"ack_observed_ns":at});return ack
    def state(role):
        ack=request(role,{"kind":"probe"});states.append({"role":role,"at":time.monotonic_ns(),"state":ack.get("state")});return ack.get("state",{})
    def state_wait(role,predicate,limit=2):
        deadline=time.monotonic()+limit
        while time.monotonic()<deadline:
            value=state(role)
            if predicate(value):return value
            time.sleep(.02)
        raise TimeoutError(role+" TUI state")
    try:
        version=subprocess.run([shutil.which("omp"),"--version"],capture_output=True,text=True,timeout=8).stdout.strip()
        tokens=bridge.tokens
        for role in ("manager","worker"):
            phase=Phase(out/role);phase.tui=True;phase.notice_ids=[]
            phase.extra_env.update({"WORKBENCH_G3_BRIDGE_SOCKET":str(socket_path),"WORKBENCH_G3_ROLE":role,
               "WORKBENCH_G3_TOKEN":tokens[role],"WORKBENCH_G3_GENERATION":"1","WORKBENCH_G3_EXPECTED_OMP_VERSION":version})
            phases[role]=phase;phase.start()
        peers={role:bridge.peer(role,timeout=8)for role in phases}
        data["hello"]={role:peer["hello"]for role,peer in peers.items()}
        data["initial_state"]={role:state_wait(role,lambda s:s.get("idle")and s.get("editorEmpty")and s.get("editorKnown"),8)for role in phases}
        worker=phases["worker"];worker.launch({})
        manager=phases["manager"]
        with TaskRepository(manager.private/"data/tasks.sqlite3")as repo:
            os.chmod(manager.private/"data/tasks.sqlite3",0o600)
            text="작업 변경 검증 근거 "*25
            req,_=_request(repo,text,sha256(skill_bytes()).hexdigest());manager.prepare("priority",req,text);reserve(repo,manager.requests["priority"])
        manager.held_case="priority";manager.launch({"priority":True})
        if not manager.held.wait(8):raise TimeoutError("product child hold")
        hold=manager.posts[0]["monotonic_ns"]
        data["held_parent_state"]=state("manager")
        os.write(manager.master,b"Q01-PRIVATE-UNSENT-DRAFT")
        data["draft_state"]=state_wait("manager",lambda s:s.get("editorEmpty")is False)
        mid=str(uuid4());manager.notice_ids.append(mid)
        mframe={"kind":"notice","notice":{"type":"worker_terminal_done","notice_id":mid,"task_id":str(uuid4()),"status":"finished"}}
        data["draft_ack"]=request("manager",mframe)
        active=ActiveRunRef(task_id=str(uuid4()),revision_id=str(uuid4()),revision=1,run_id=str(uuid4()),session_id=str(peers["worker"]["session_id"]),session_generation=int(peers["worker"]["generation"]))
        automation={"portVersion":2,"kind":"AutomationState","payload":{"paused":False,"cancelled":False,"metadataHealthy":True,"approvalValid":True}}
        admission=SerializedReviewAdmission(active,automation);clock=[0.0];dispatched=[]
        def dispatch(review):
            nid=str(uuid4());worker.notice_ids.append(nid);enqueue=time.monotonic_ns()
            frame={"kind":"notice","notice":{"type":"terminal_check","notice_id":nid,"task_id":review.run.task_id,"run_id":review.run.run_id,"due_at":review.due_at}}
            ack=request("worker",frame);dispatched.append({"notice_id":nid,"enqueue_ns":enqueue,"ack":ack})
            wait(worker,lambda:any(p.get("notice_id")==nid for p in worker.posts),2)
            wait(worker,lambda:bridge.event_count("worker","agent_end")>=1,2)
            return {"status":"omp_processed"if ack.get("status")=="api_accepted"else"deferred"}
        scheduler=WorkerReviewScheduler(admission=admission,automation_state=lambda:automation,active_run=lambda:active,
          worker_state=lambda _r:state("worker"),collect_non_model=lambda _r:{"phase":"running","process_alive":True,"exit_confirmed":False,"raw_log_excerpt":"private owned fixture","unknowns":[]},
          dispatch_review=dispatch,user_priority=lambda _r:False,on_exit=lambda e:None,clock=lambda:clock[0])
        t0=scheduler.tick();clock[0]=60;t60=scheduler.tick();data["scheduler"]={"tick0":t0.status,"tick60":t60.status,"notice_dispatches":dispatched}
        os.write(manager.master,b"\x15");data["clear_state"]=state_wait("manager",lambda s:s.get("editorEmpty")is True)
        menqueue=time.monotonic_ns();data["end_ack"]=request("manager",mframe)
        wait(manager,lambda:any(p.get("notice_id")==mid for p in manager.posts),2)
        wait(manager,lambda:bridge.event_count("manager","agent_end")>=1,2)
        release=time.monotonic_ns();manager.release.set()
        result=manager.result("priority");manager.advance("priority")
        wait(manager,lambda:any(e.get("event")=="done"for e in manager.events()))
        sessions={role:peers[role]["hello"]["ompSessionId"]for role in phases}
        sessions["child"]=next(e["childSessionId"]for e in manager.events()if e.get("event")=="actual_created")
        windows=[]
        for role,kind,nid,enqueue in (("manager","worker_terminal_done",mid,menqueue),("worker","terminal_check",dispatched[0]["notice_id"],dispatched[0]["enqueue_ns"])):
            rec=next(r for r in records if r["role"]==role and r["frame"].get("notice",{}).get("notice_id")==nid and r["ack"].get("status")=="api_accepted")
            post=next(p for p in phases[role].posts if p.get("notice_id")==nid)
            starts=[round(e["observedAt"]*1e9)for e in bridge.events if e.get("role")==role and e.get("name")=="agent_start"and e["observedAt"]*1e9>=enqueue]
            ends=[round(e["observedAt"]*1e9)for e in bridge.events if e.get("role")==role and e.get("name")=="agent_end"and e["observedAt"]*1e9>=enqueue]
            if len(starts)!=1 or len(ends)!=1:raise RuntimeError("native turn attribution not unique")
            start,end=starts[0],ends[0];ack=rec["ack_observed_ns"];posted=post["monotonic_ns"]
            windows.append({"role":role,"kind":kind,"session_id":sessions[role],"notice_id":nid,"ack_notice_id":rec["ack"].get("noticeId"),"ack_request_id":rec["ack"].get("requestId"),"ack_status":rec["ack"].get("status"),"provider_notice_id":nid,"provider_stage":"parent","scheduler_notice_id":nid if role=="worker"else None,
              "enqueue_ns":enqueue,"frame_request_ns":rec["sent_ns"],"ack_observed_ns":ack,"agent_start_ns":start,"provider_post_ns":posted,"agent_end_ns":end,
              "enqueue_to_ack_ms":(ack-enqueue)/1e6,"enqueue_to_start_ms":(start-enqueue)/1e6,"enqueue_to_post_ms":(posted-enqueue)/1e6,"turn_hold_overlap_ms":max(0,min(end,release)-max(start,hold))/1e6})
        measure={"kind":"delivery_turn_overlap","clock":"time.monotonic_ns","session_ids":sessions,"child_hold_interval_ns":[hold,release],"internal_lock_wait":"unavailable","due_scheduler_status":t60.status,"notice_windows":windows,"max_enqueue_to_start_ms":max(w["enqueue_to_start_ms"]for w in windows),"max_enqueue_to_post_ms":max(w["enqueue_to_post_ms"]for w in windows)}
        from tools.q01_recheck import nonshared_priority_issues
        issues=nonshared_priority_issues(measure)
        data.update(measurement=measure,measurement_issues=issues,typed_result=result)
        idle=data["held_parent_state"]
        checks={"busy_deferred":data["draft_ack"].get("status")=="deferred","user_priority":data["draft_state"].get("editorEmpty")is False and data["end_ack"].get("status")=="api_accepted",
          "due_check_priority":t60.status=="dispatched"and not issues,"end_priority":not issues,
          "max_block_recorded":None,"shared_session_or_lock":False if not issues and idle.get("idle")and not idle.get("pending")and idle.get("inFlightToolCount")==0 else None,
          "measured_block_ms":None,"independent_delivery_observed":not issues,"nonshared_priority_measurement":measure}
        data["checks"]=checks
    except Exception as e:data["error"]=f"{type(e).__name__}: {e}"
    finally:
        for phase in phases.values():phase.release.set();phase.close()
        data["phases"]={role:phase.snapshot()for role,phase in phases.items()}
        data["requests"]=records;data["states"]=states;data["bridge_events"]=list(bridge.events)
        bridge.shutdown();bridge.server_close();server.join(2)
        data["server_stopped"]=not server.is_alive()
        data["private_socket_path"]=str(socket_path)
        if socket_path.exists():socket_path.unlink()
        socket_dir.rmdir();data["socket_cleaned"]=not socket_dir.exists()
    (out/"result.json").write_text(json.dumps(data,ensure_ascii=False,indent=2));return data
