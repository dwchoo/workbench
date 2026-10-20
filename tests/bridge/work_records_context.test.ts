import assert from "node:assert/strict";
import { test } from "node:test";
import { recordsContext } from "../../omp_bridge/g3/records_context.ts";
const row = (title="결정 α", preview="원문 😀") => ({record_id:"11111111-1111-4111-8111-111111111111",
    revision:2,title,preview,preview_truncated:false,task_id:null,created_at:"2026-10-20T01:02:03Z",
    recorded_at:"2026-10-20T01:03:03Z",actor:"manager"});
function fixture(timeout=100) {
    const messages:any[]=[];
    const pi={sendMessage:(message:any, options:any)=>{messages.push({message,options});}};
    return {messages, context:recordsContext(pi,timeout)};
}
test("ready primary preview awaits bounded first input without creating a model turn", async()=>{
    const {messages,context}=fixture(); let resolve!:(v:any)=>void;
    context.start("session-A",1);
    context.ready("connection-A",()=>new Promise(r=>{resolve=r;}),()=>true);
    let ready=false;
    const input=context.beforeInput().then(()=>{ready=true;});
    await Promise.resolve();assert.equal(ready,false);
    resolve({status:"ok",records:[row()],has_more:false,omitted:false,context_truncated:false});
    await input;
    assert.equal(messages.length,1);
    assert.equal(messages[0].message.display,true);
    assert.deepEqual(messages[0].options,{triggerTurn:false});
    assert.match(messages[0].message.content,/결정 α/);
    assert.match(messages[0].message.content,/참고 데이터/);
    context.stop();
});

test("missing ready or unavailable recent data releases input with distinct visible failure", async()=>{
    const {messages,context}=fixture(20);
    context.start("offline-session",1);
    const result=await Promise.race([context.beforeInput().then(()=>"released"),new Promise(r=>setTimeout(()=>r("stuck"),70))]);
    assert.equal(result,"released");
    assert.match(messages[0].message.content,/조회할 수 없습니다/);
    context.stop();
});

test("recent context is at most five 160-code-point previews and 16 KiB, with omissions explicit", async()=>{
    const {messages,context}=fixture();context.start("bounded",1);
    context.ready("one",async()=>({status:"ok",records:Array.from({length:6},()=>({...row("한글","😀".repeat(170)),body:"FULL_BODY_MUST_NOT_REACH_CONTEXT"})),has_more:false}),()=>true);
    await context.beforeInput();
    const text=messages[0].message.content;
    const result=JSON.parse(text.slice(text.indexOf("\n")+1));
    assert.equal(result.records.length,5);
    assert.equal([...result.records[0].preview].length,160);
    assert.equal(result.records[0].preview_truncated,true);
    assert.equal(result.omitted,true);
    assert.ok(Buffer.byteLength(JSON.stringify(messages[0].message))<=16384);
    assert.ok(!text.includes("FULL_BODY_MUST_NOT_REACH_CONTEXT"));
    context.start("byte-bound",2);
    context.ready("two",async()=>({status:"ok",records:[{...row(),task_id:"t".repeat(16384)}],has_more:false}),()=>true);
    await context.beforeInput();
    const bounded=messages.at(-1).message;
    assert.ok(Buffer.byteLength(JSON.stringify(bounded))<=16384);
    const omitted=JSON.parse(bounded.content.slice(bounded.content.indexOf("\n")+1));
    assert.equal(omitted.records.length,0);assert.equal(omitted.omitted,true);assert.equal(omitted.context_truncated,true);
    context.stop();
});

test("same lifecycle identity and reconnect inject once; switched and disconnected results never leak", async()=>{
    const {messages,context}=fixture(60);let oldResolve!:(v:any)=>void;let socket="old";let calls=0;
    context.start("A",1);
    context.ready("old",()=>new Promise(r=>{oldResolve=r;}),()=>socket==="old");
    context.start("B",2);socket="new";
    const read=async()=>{calls++;return {status:"ok",records:[row("NEW_ONLY")],has_more:false};};
    context.ready("new",read,()=>socket==="new");await context.beforeInput();
    oldResolve({status:"ok",records:[row("STALE_ONLY")]});await Promise.resolve();
    context.start("B",2); // repeated notification is not a new session
    context.ready("reconnected",read,()=>true);await context.beforeInput();
    assert.equal(calls,1);assert.equal(messages.length,1);assert.ok(!messages[0].message.content.includes("STALE_ONLY"));
    context.stop();
});

test("production bridge ready roundtrips recent over its bound socket before first input",async()=>{
    const net=await import("node:net");const fs=await import("node:fs/promises");
    const {tmpdir}=await import("node:os");const {join}=await import("node:path");
    const {default:extension}=await import("../../omp_bridge/g3/bridge.ts");
    const directory=await fs.mkdtemp(join(tmpdir(),"wr06-wiring-"));const path=join(directory,"b.sock");
    const frames:any[]=[];let socket:any;const handlers=new Map();const messages:any[]=[];
    const previous={...process.env};
    const server=net.createServer(client=>{socket=client;let pending="";client.on("data",chunk=>{
        pending+=chunk.toString();while(pending.includes("\n")){
            const index=pending.indexOf("\n"),frame=JSON.parse(pending.slice(0,index));pending=pending.slice(index+1);frames.push(frame);
            if(frame.kind==="hello")client.write(JSON.stringify({kind:"ready",sessionId:frame.ompSessionId,generation:frame.generation})+"\n");
            if(frame.kind==="tool_request")client.write(JSON.stringify({kind:"tool_result",requestId:frame.requestId,toolCallId:frame.toolCallId,
                result:{status:"ok",records:[row("PRODUCTION_WIRING")],has_more:false}})+"\n");
        }
    });});
    await new Promise<void>(r=>server.listen(path,r));
    Object.assign(process.env,{WORKBENCH_G3_BRIDGE_SOCKET:path,WORKBENCH_G3_ROLE:"manager",WORKBENCH_G3_TOKEN:"synthetic",WORKBENCH_G3_GENERATION:"1"});
    const errors:any[]=[];
    try{
        extension({on:(name:any,handler:any)=>handlers.set(name,handler),registerTool:()=>{},
            sendMessage:(message:any,options:any)=>messages.push({message,options}),logger:{error:(...args:any[])=>errors.push(args)}});
        handlers.get("session_start")(undefined,{agent:{kind:"main",depth:0},sessionManager:{getSessionId:()=>"session-1"},
            isIdle:()=>true,hasPendingMessages:()=>false,ui:{getEditorText:()=>""}});
        await handlers.get("input")({source:"interactive",text:"hello"});
        assert.equal(frames.filter(f=>f.kind==="tool_request"&&f.args.action==="recent").length,1,JSON.stringify({frames,errors}));
        assert.match(messages[0].message.content,/PRODUCTION_WIRING/);
    }finally{
        handlers.get("session_shutdown")?.();socket?.destroy();await new Promise<void>(r=>server.close(()=>r()));
        for(const key of Object.keys(process.env))if(!(key in previous))delete process.env[key];Object.assign(process.env,previous);
        await fs.rm(directory,{recursive:true,force:true});
    }
});

test("empty, omitted-only, unavailable and disconnected responses stay distinct",async()=>{
    const {messages,context}=fixture(20);
    context.start("empty",1);context.ready("one",async()=>({status:"ok",records:[],omitted:false}),()=>true);await context.beforeInput();
    assert.match(messages.at(-1).message.content,/최근 기록이 없습니다/);
    context.start("omitted",2);context.ready("two",async()=>({status:"ok",records:[],omitted:true,context_truncated:true}),()=>true);await context.beforeInput();
    assert.ok(!messages.at(-1).message.content.includes("최근 기록이 없습니다"));
    assert.match(messages.at(-1).message.content,/생략/);
    context.start("unavailable",3);context.ready("three",async()=>({status:"unavailable"}),()=>true);await context.beforeInput();
    assert.match(messages.at(-1).message.content,/조회할 수 없습니다/);
    context.start("disconnected",4);context.ready("old",async()=>({status:"ok",records:[row("DISCONNECTED_SECRET")]}),()=>false);await context.beforeInput();
    assert.ok(!JSON.stringify(messages).includes("DISCONNECTED_SECRET"));
    context.stop();
});
