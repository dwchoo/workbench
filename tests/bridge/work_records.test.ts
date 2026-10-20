// Actual UDS and stand-in OMP registration boundary; no provider or user state.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import workbenchG3Extension from "../../omp_bridge/g3/bridge.ts";

type Frame = Record<string, any>;
const SESSION = "30000000-0000-4000-8000-000000000002";
const RECORD = "11111111-1111-4111-8111-111111111111";
function args(action: string, changes: Frame = {}): Frame {
    return {action, record_id:null, revision:null, expected_revision:null, title:null, body:null,
        task_id:null, query:null, limit:null, after:null, output_path:null, ...changes};
}
function resultOf(result: Frame): Frame {
    const text = JSON.parse(result.content[0].text);
    assert.deepEqual(result.details, text);
    return text;
}
async function fixture(role: "manager" | "worker", agent: Frame = {kind:"main",depth:0}) {
    const directory = await mkdtemp(join(tmpdir(), "wr05-tool-"));
    const path = join(directory, "bridge.sock");
    const frames: Frame[] = [];
    const sockets: net.Socket[] = [];
    const server = net.createServer(socket => {
        sockets.push(socket);
        let buffer = "";
        socket.on("data", data => {
            buffer += data.toString("utf8");
            while (buffer.includes("\n")) {
                const end = buffer.indexOf("\n");
                frames.push(JSON.parse(buffer.slice(0,end)));
                buffer = buffer.slice(end+1);
            }
        });
    });
    await new Promise<void>((resolve,reject) => {server.once("error",reject);server.listen(path,resolve);});
    const env = {WORKBENCH_G3_BRIDGE_SOCKET:path,WORKBENCH_G3_ROLE:role,
        WORKBENCH_G3_TOKEN:randomUUID(),WORKBENCH_G3_GENERATION:"1"};
    const previous = new Map(Object.keys(env).map(key => [key,process.env[key]]));
    Object.assign(process.env,env);
    const tools = new Map<string,Frame>();
    const handlers = new Map<string,Function>();
    const active: string[][] = [];
    workbenchG3Extension({on:(name:string,fn:Function)=>handlers.set(name,fn),
        registerTool:(tool:Frame)=>tools.set(tool.name,tool),
        getActiveTools:()=>["read",...tools.keys()],setActiveTools:(names:string[])=>active.push(names),
        logger:{error() {}}});
    const ctx = {agent,sessionManager:{getSessionId:()=>SESSION},isIdle:()=>true,hasPendingMessages:()=>false,
        ui:{getEditorText:()=>""},abort:()=>{}};
    handlers.get("session_start")!(undefined,ctx);
    async function wait(kind: string, timeout=2500): Promise<Frame> {
        const deadline=Date.now()+timeout;
        while(Date.now()<deadline) {
            const index=frames.findIndex(x=>x.kind===kind);
            if(index>=0)return frames.splice(index,1)[0];
            await delay(5);
        }
        throw new Error(`missing ${kind}`);
    }
    if(agent.kind!=="sub")await wait("hello");
    return {tools,frames,sockets,handlers,active,ctx,wait,
        reply(request:Frame,result:Frame) {sockets.at(-1)!.write(JSON.stringify({kind:"tool_result",
            requestId:request.requestId,toolCallId:request.toolCallId,result})+"\n");},
        async close() {
            handlers.get("session_shutdown")?.();
            sockets.forEach(socket=>socket.destroy());
            await new Promise<void>(resolve=>server.close(()=>resolve()));
            for(const [key,value] of previous)if(value===undefined)delete process.env[key];else process.env[key]=value;
            await rm(directory,{recursive:true,force:true});
        }};
}

test("both primary roles get the essential strict records tool and all actions roundtrip once", async () => {
    const cases: [Frame,Frame][] = [
        [args("create",{title:"메모",body:'# 한글\n"원문" 😀\n'}),{status:"saved",record:{record_id:RECORD,revision:1}}],
        [args("revise",{record_id:RECORD,expected_revision:1,title:"수정",body:"정정\n"}),{status:"saved",record:{record_id:RECORD,revision:2}}],
        [args("show",{record_id:RECORD,revision:1}),{status:"ok",record:{record_id:RECORD,revision:1,body:"이전 원문\n"}}],
        [args("list"),{status:"ok",records:[],has_more:false,next_after:null}],
        [args("search",{query:"% _"}),{status:"ok",records:[],has_more:false,next_after:null}],
        [args("history",{record_id:RECORD}),{status:"ok",revisions:[],has_more:false,next_after:null}],
        [args("recent"),{status:"ok",records:[],has_more:false,omitted:false,context_truncated:false}],
        [args("export",{record_id:RECORD,output_path:"notes.md"}),{status:"exported",record_id:RECORD,revision:2,path:"/host/notes.md"}],
    ];
    for(const role of ["manager","worker"] as const) {
        const bridge=await fixture(role);
        try {
            const tool=bridge.tools.get("workbench_records");
            assert.ok(tool,"records tool is registered");
            assert.equal(tool.strict,true);assert.equal(tool.loadMode,"essential");
            assert.equal(tool.parameters.additionalProperties,false);
            assert.deepEqual([...tool.parameters.required].sort(),Object.keys(args("recent")).sort());
            assert.deepEqual([...tool.parameters.properties.action.enum].sort(),["create","export","history","list","recent","revise","search","show"]);
            for(const [input,expected] of cases) {
                const pending=tool.execute(`call-${input.action}`,{...input,i:"OMP injected intent"},undefined,undefined,bridge.ctx);
                const frame=await bridge.wait("tool_request");
                assert.equal(frame.tool,"workbench_records");assert.equal(frame.sessionId,SESSION);assert.equal(frame.generation,1);
                assert.deepEqual(frame.args,input);
                bridge.reply(frame,expected);
                assert.deepEqual(resultOf(await pending),expected);
                assert.equal(bridge.frames.filter(x=>x.kind==="tool_request").length,0);
            }
        } finally {await bridge.close();}
    }
});

test("records tools are hidden and refused in subagent sessions and shared main-context calls", async () => {
    for(const role of ["manager","worker"] as const) {
        const sub=await fixture(role,{kind:"sub",depth:1,parentId:"parent"});
        try {
            assert.deepEqual(sub.active,[["read"]]);
            const result=resultOf(await sub.tools.get("workbench_records")!.execute("sub",args("recent"),undefined,undefined,sub.ctx));
            assert.deepEqual([result.status,result.reason],["rejected","subagent_not_allowed"]);
            assert.equal(sub.sockets.length,0);
        } finally {await sub.close();}
        const main=await fixture(role);
        try {
            const result=resultOf(await main.tools.get("workbench_records")!.execute("shared-sub",args("recent"),undefined,undefined,
                {...main.ctx,agent:{kind:"sub",depth:1,parentId:"parent"}}));
            assert.equal(result.reason,"subagent_not_allowed");
            assert.equal(main.frames.filter(x=>x.kind==="tool_request").length,0);
        } finally {await main.close();}
    }
});

test("unexpected actor root session fields are refused locally without a wire request", async () => {
    const bridge=await fixture("manager");
    try {
        for(const changed of [{actor:"worker"},{root:"/other/project"},{session_id:SESSION},{limit:1}]) {
            const result=resultOf(await bridge.tools.get("workbench_records")!.execute("bad",args("create",{
                title:"메모",body:"original",...changed}),undefined,undefined,bridge.ctx));
            assert.deepEqual(result,{status:"rejected",reason:"invalid_arguments"});
        }
        assert.equal(bridge.frames.filter(x=>x.kind==="tool_request").length,0);
    } finally {await bridge.close();}
});

test("write timeout and disconnect stay outcome_unknown and never replay after reconnect", async () => {
    const bridge=await fixture("worker");
    try {
        const tool=bridge.tools.get("workbench_records")!;
        const timed=tool.execute("timeout-write",args("create",{title:"메모",body:"timeout sentinel"}),undefined,undefined,bridge.ctx);
        const original=await bridge.wait("tool_request");
        const timeout=resultOf(await timed);
        assert.deepEqual(timeout,{status:"outcome_unknown",reason:"timeout"});
        bridge.reply(original,{status:"saved",record:{record_id:RECORD,revision:1}});
        await delay(30);
        assert.equal(bridge.frames.filter(x=>x.kind==="tool_request").length,0);
        const dropped=tool.execute("disconnect-write",args("revise",{record_id:RECORD,expected_revision:1,
            title:"수정",body:"disconnect sentinel"}),undefined,undefined,bridge.ctx);
        await bridge.wait("tool_request");
        bridge.sockets.at(-1)!.destroy();
        assert.deepEqual(resultOf(await dropped),{status:"outcome_unknown",reason:"bridge_disconnected"});
        await bridge.wait("hello");
        await delay(50);
        assert.equal(bridge.frames.filter(x=>x.kind==="tool_request").length,0);
    } finally {await bridge.close();}
});
