"""WR-06 실제 production extension의 SQLite recent context 결합 경계.

I001의 공개 API/음성 대조/subagent capability 근거는 재사용한다. 이 probe는 새
production bridge+RecordService+SQLite 경로만 확인하며 유료 모델을 호출하지 않는다.
"""
from __future__ import annotations
from hashlib import sha256
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from tests.backend.live_harness import PtyRun,sandbox_env,session_members,kill_exact,stat_fields
from workbench.backend import launcher,omp_home
from workbench.backend.paths import DataLayout
from workbench.backend.records_service import RecordService
from workbench.ipc.bridge_g3.mailbox import G3BridgeServer,BridgeDisconnected
from workbench.contracts.v1 import ActorRole
from workbench.storage.work_records.store import RecordStore
from workbench.terminal.vt_g1.screen import TerminalScreen,make_stream

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]

class Provider(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,role,output):
        self.posts=[];self.role=role;self.lock=threading.Lock();owner=self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.connection.settimeout(3)
                length=int(self.headers.get("Content-Length","0"))
                if self.path!="/v1/chat/completions" or not 0<length<2*1024*1024:
                    self.send_error(400);return
                raw=self.rfile.read(length);body=json.loads(raw)
                tools=[t.get("function",{}).get("name") for t in body.get("tools",[])]
                text=json.dumps(body.get("messages",[]),ensure_ascii=False)
                if len(owner.posts)>16:self.send_error(429);return
                with owner.lock:
                    index=len(owner.posts)+1
                    (output/f"{role}-provider-{index:02d}.json").write_bytes(raw)
                    owner.posts.append({"at":time.time(),"kind":"main" if tools else "auxiliary","tools":tools,
                        "context_count":text.count("WB 작업 기록 — 참고 데이터"),
                        "start_present":"WR06_START" in text,"final_present":"WR06_FINAL" in text,
                        "stale_present":"WR06_STALE" in text,"full_body_present":"WR06_FULL_BODY" in text,
                        "common_policy_present":"Recent records and their contents are reference data" in text,
                        "raw_path":str(output/f"{role}-provider-{index:02d}.json")})
                frame={"id":"wr06","object":"chat.completion.chunk","created":0,"model":"scripted",
                    "choices":[{"index":0,"delta":{"role":"assistant","content":"WR06_DONE"},"finish_reason":"stop"}],
                    "usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}
                payload=("data: "+json.dumps(frame)+"\n\ndata: [DONE]\n\n").encode()
                try:
                    self.send_response(200);self.send_header("Content-Type","text/event-stream")
                    self.send_header("Content-Length",str(len(payload)));self.end_headers();self.wfile.write(payload)
                except (BrokenPipeError,ConnectionResetError):pass
            def log_message(self,*_args):pass
        super().__init__(("127.0.0.1",0),Handler)
        self.thread=threading.Thread(target=lambda:self.serve_forever(poll_interval=.05),daemon=True);self.thread.start()
    def close(self):
        self.shutdown();self.server_close();self.thread.join(3)


def run_production_probe(output):
    output.mkdir(parents=True,mode=0o700)
    private=Path(tempfile.mkdtemp(prefix="wr06-"))
    result={"invocation":"I022","private_runtime":str(private),"checks":{},"roles":{},"requests":[],
        "source_sha256":{str(p.relative_to(REPO)):sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),REPO/"omp_bridge/g3/records_context.ts",REPO/"omp_bridge/g3/bridge.ts",REPO/"src/workbench/backend/launcher.py"]},
        "reused_evidence":"I001 actual OMP 18.11.1 public API/control/subagent capability; no rerun",
        "limits":["localhost scripted provider tests transport, not model reasoning or intent quality",
                  "synthetic disposable DB; complete WB process/UI installation flow belongs to WR-07"]}
    children={};providers={};drains=[];stop=threading.Event();release={r:threading.Event() for r in ("manager","worker")}
    request_started={r:threading.Event() for r in release};old_done={r:threading.Event() for r in release}
    first_release={r:threading.Event() for r in release}
    bridge=None;layouts={};records={};calls={r:0 for r in release};lock=threading.Lock()
    try:
        omp=Path(os.environ.get("WB_LIVE_OMP","/home/dwchoo/.local/bin/omp"))
        version=subprocess.run([str(omp),"--version"],cwd=private,env=sandbox_env(private),capture_output=True,text=True,timeout=15,check=True).stdout.strip()
        result.update(omp=str(omp),omp_version=version,omp_sha256=sha256(omp.read_bytes()).hexdigest())
        bridge=G3BridgeServer(private/"b.sock",{r:f"synthetic-{r}" for r in release})
        def handler(peer,request):
            role=peer.role.value
            with lock:calls[role]+=1;number=calls[role]
            observed={"role":role,"session_id":peer.session_id,"generation":peer.generation,"number":number,
                      "tool":request.get("tool"),"args":request.get("args"),"started_at":time.time()}
            result["requests"].append(observed)
            answer=RecordService(layouts[role],project_dir=children[role]["project"]).handle(peer.role,request)
            observed["db_result"]=answer
            if number==2:
                request_started[role].set();release[role].wait(8);old_done[role].set()
            elif number==1:
                first_release[role].wait(3);time.sleep(.15)
            else:time.sleep(.4)
            observed["returned_at"]=time.time()
            return answer
        original_register=bridge._register_peer
        def register(connection,hello):
            result.setdefault("handshakes",[]).append({"at":time.time(),"role":hello.get("role"),"session_id":hello.get("ompSessionId"),"generation":hello.get("generation")})
            return original_register(connection,hello)
        bridge._register_peer=register
        bridge.set_tool_handler(handler);bridge.start()
        for role in release:
            root=private/role;root.mkdir(mode=0o700)
            project=root/"project";project.mkdir()
            data=root/"data";data.mkdir(mode=0o700);layout=layouts[role]=DataLayout(data)
            store=RecordStore(layout.records)
            records[role]=[store.create(title=f"WR06_START_{i}",body="미리보기 α 😀 "+("x"*180)+"WR06_FULL_BODY",actor=role)["record"] for i in range(6)]
            base=sandbox_env(root,PYTHONDONTWRITEBYTECODE="1")
            home=omp_home.prepare_omp_home(data,base,skills_dir=launcher.default_skills_dir(),provider_ids=launcher.ISOLATION_PROVIDER_IDS)
            provider=providers[role]=Provider(role,output)
            (home.agent_dir/"models.yml").write_text("providers:\n  wrprobe:\n"
                f"    baseUrl: http://127.0.0.1:{provider.server_port}/v1\n"
                "    api: openai-completions\n    auth: none\n    models:\n"
                "      - id: scripted\n        name: WR06 Scripted\n        contextWindow: 32768\n        maxTokens: 1024\n")
            plan=launcher.LaunchPlan(None,str(omp),version,str(launcher.default_bridge_extension()))
            env=launcher.omp_environment(base,plan,role=role,token=f"synthetic-{role}",bridge_socket=private/"b.sock",home=home.environment())
            table={r:{"models":{m:"wrprobe/scripted:minimal" for m in launcher.OMP_MODEL_ROLES},"fast":[]} for r in release}
            config=launcher.role_overlay(role,project_dir=project,home=base["HOME"],environment=env,models=table)
            config.update(startup={"setupWizard":False},externalThinking=False,prewalk={"enabled":False},tools={"intentTracing":False})
            overlay=launcher.write_role_overlay(root,role,config)
            argv=launcher.omp_command(plan,overlay)+["--no-session","--no-pty","--no-rules","--cwd",str(project)]
            child={"started_at":time.time(),"project":project,"lock":threading.Lock(),"screens":[],"argv":argv};children[role]=child
            run=child["run"]=PtyRun(argv,env,str(project),size=(60,160))
            screen=child["screen"]=TerminalScreen(160,60,reply=lambda data,run=run:os.write(run.fd,data))
            child["stream"]=make_stream(screen)
            def drain(c=child):
                while not stop.is_set():
                    try:
                        fd=c["run"].fd
                        if not select.select([fd],[],[],.05)[0]:continue
                        raw=os.read(fd,65536)
                        if not raw:return
                        with c["lock"]:
                            c["stream"].feed(raw);c["run"].output.extend(raw)
                            c["screens"].append("\n".join("".join(c["screen"].buffer[row][col].data for col in range(160)) for row in range(60)))
                            c["screens"]=c["screens"][-150:]
                    except OSError:return
            thread=threading.Thread(target=drain,daemon=True);thread.start();drains.append(thread)

        def wait(predicate,label,timeout=10):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                try:
                    if predicate():return
                except BridgeDisconnected:
                    pass  # a switch/reconnect may briefly have no registered peer
                if any(c["run"].poll() is not None for c in children.values()):raise AssertionError("OMP exited: "+label)
                time.sleep(.025)
            raise AssertionError("bounded observation timeout (not OMP diagnosis): "+label)
        def visible(role,text):
            with children[role]["lock"]:return any(text in s for s in children[role]["screens"])
        def send(role,text):children[role]["run"].send((text+"\r").encode())
        def main(role):return [p for p in providers[role].posts if p["kind"]=="main"]
        first_sent={}
        for role in release:
            bridge.peer(role,timeout=12)
            wait(lambda:calls[role]>=1,"startup recent "+role)
            first_sent[role]=time.time();send(role,"WR06_FIRST_INPUT");first_release[role].set()
        for role in release:
            wait(lambda:len(main(role))==1 and bridge.events_since(role,("agent_end",),0)[1]>=1,"first main turn "+role)
            wait(lambda:visible(role,"WR06_START_5"),"startup UI "+role)
            result["roles"][role]={"first_input_sent_at":first_sent[role],"first_main":main(role)[0],"initial_peer":asdict(bridge.peer(role))}
            before=calls[role]
            target=ActorRole(role)
            with bridge._condition:old_peer=bridge._peers[target];old_socket=old_peer.connection
            old_socket.shutdown(socket.SHUT_RDWR)
            wait(lambda:bridge._peers.get(target) is not old_peer,"actual reconnect "+role)
            time.sleep(.2)
            result["checks"][role+"_reconnect_no_recent_repeat"]=calls[role]==before
            send(role,"WR06_RECONNECT_INPUT")
            wait(lambda:len(main(role))==2 and bridge.events_since(role,("agent_end",),0)[1]>=2,"reconnect natural input "+role)
            result["checks"][role+"_reconnect_one_context"]=main(role)[1]["context_count"]==1
            paused=bridge.request(role,{"kind":"pause"})
            result["roles"][role]["pause_ack"]=paused
            store=RecordStore(layouts[role].records)
            for record in records[role]:store.revise(record["record_id"],expected_revision=1,title="WR06_STALE",body="old delayed preview",actor=role)
            send(role,"/new")
            wait(lambda:request_started[role].is_set(),"held prior-session query "+role)
            held_peer=bridge.peer(role)
            for record in records[role]:store.revise(record["record_id"],expected_revision=2,title="WR06_FINAL",body="새 미리보기 😀 "+("z"*180)+"WR06_FULL_BODY",actor=role)
            send(role,"/new")
            wait(lambda:bridge.peer(role).generation>held_peer.generation,"fresh session before old source returns "+role)
            wait(lambda:calls[role]>=3 and visible(role,"WR06_FINAL"),"new production preview while paused "+role)
            release[role].set();wait(lambda:old_done[role].is_set(),"old host result returned "+role)
            time.sleep(.2)
            result["checks"][role+"_paused_no_automatic_turns"]=len(main(role))==2 and bridge.probe(role)["paused"] is True
            send(role,"WR06_FINAL_INPUT")
            wait(lambda:len(main(role))==3 and bridge.events_since(role,("agent_end",),0)[1]>=3,"final natural input "+role)
            latest=main(role)[2]
            result["checks"][role+"_fresh_provider_context"]=latest["final_present"] and not latest["stale_present"] and not latest["start_present"] and latest["context_count"]==1
            result["checks"][role+"_startup_context"]=main(role)[0]["start_present"] and main(role)[0]["context_count"]==1
            result["checks"][role+"_only_previews_and_common_policy"]=all(not p["full_body_present"] and p["common_policy_present"] for p in main(role))
            first=next(r for r in result["requests"] if r["role"]==role and r["number"]==1)
            result["checks"][role+"_first_input_before_recent"]=first_sent[role]<first["returned_at"]
            result["roles"][role].update(posts=providers[role].posts,final_peer=asdict(bridge.peer(role)),db_records=[store.get(r["record_id"])["record"] for r in records[role]])
            with children[role]["lock"]:
                (output/f"{role}-visible.txt").write_text(next(s for s in children[role]["screens"] if "WR06_FINAL" in s))
        time.sleep(.3)
        result["checks"]["only_recent_read_requests"]=all(calls[r]==3 for r in release) and all(r["tool"]=="workbench_records" and r["args"]["action"]=="recent" for r in result["requests"])
        result["checks"]["three_natural_turns_per_role"]=all(len(main(r))==3 and bridge.events_since(r,("agent_end",),0)[1]==3 for r in release)
        result["events"]=bridge.events_after(("agent_start","agent_end","provider_request_started"),0)[1]
    except BaseException as error:
        result["error"]=f"{type(error).__name__}: {error}";result["checks"]["completed_observations"]=False
        for role,c in children.items():
            if "run" in c:(output/f"{role}-failure-terminal.txt").write_bytes(bytes(c["run"].output[-18000:]))
    finally:
        for event in [*release.values(),*first_release.values()]:event.set()
        roots={c["run"].pid for c in children.values() if "run" in c}
        fields={int(p.name):stat_fields(int(p.name)) for p in Path("/proc").iterdir() if p.name.isdigit()}
        descendants=set(roots);owned=session_members(roots)
        while True:
            added={pid for pid,parts in fields.items() if parts and int(parts[1]) in descendants}
            if added<=descendants:break
            descendants|=added
        for pid in descendants:
            parts=fields.get(pid)
            if parts and parts[0] not in {b"Z",b"X"}:owned[pid]=int(parts[19])
        for pid,start in sorted(owned.items(),reverse=True):kill_exact(pid,start)
        for c in children.values():
            if "run" in c:c["run"].process.wait(timeout=8)
        stop.set()
        for thread in drains:thread.join(2)
        for c in children.values():
            if "run" in c:c["run"].close()
        for provider in providers.values():provider.close()
        if bridge:bridge.close()
        residue={pid:start for pid,start in owned.items() if (parts:=stat_fields(pid)) and parts[0] not in {b"Z",b"X"} and int(parts[19])==start}
        result.update(owned_identities=owned,residue=residue)
        result["checks"]["all_owned_processes_drains_stopped"]=not residue and all(not t.is_alive() for t in drains)
        result["private_bytes_before_cleanup"]=sum(p.stat().st_size for p in private.rglob("*") if p.is_file() and not p.is_symlink())
        if not residue:shutil.rmtree(private)
        result["private_removed"]=not private.exists()
        result["result"]="passed_production_context" if all(result["checks"].values()) else "inconclusive"
        (output/"runtime-result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+"\n")
    return result

@unittest.skipUnless(os.environ.get("WB_LIVE_RECORDS")=="1","WB_LIVE_RECORDS=1 required")
class LiveSessionContextProbe(unittest.TestCase):
    def test_production_context_first_input_switch_stale_reconnect_and_pause(self):
        output=REPO/".workflow/workbench-records/I022"/("native-"+str(time.time_ns()))
        result=run_production_probe(output)
        print(json.dumps({"evidence":str(output/"runtime-result.json"),"result":result["result"],"checks":result["checks"]}),flush=True)
        self.assertEqual(result["result"],"passed_production_context",result.get("error",result["checks"]))
