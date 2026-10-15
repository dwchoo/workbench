/** DVL10 independent budget-cause tests. Controlled SDK, no runtime/POST claim. */
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { test } from "node:test";
import { summaryInputKey } from "../../omp_bridge/contract/logs_v1.ts";
import { dispatchSummarySDK } from "../../omp_bridge/g3/summary_sdk.ts";

const digest = (text: string) => createHash("sha256").update(text).digest("hex");
const definition = "Existing agent definition in a controlled budget test.";
function ownedRequest() {
  const subject = { schemaVersion: 1, kind: "command", id: randomUUID() };
  const span = { schemaVersion: 1, source: { schemaVersion: 1, sourceKind: "terminal",
    commandId: subject.id, collectorEpoch: 1 }, start: 0, end: 4, digest: digest("work") };
  const skillDigest = digest(readFileSync("omp_bridge/skills/important-log-summary/SKILL.md", "utf8"));
  const parent = randomUUID();
  const metadata = { source: "parent_session_readback", parentSessionId: parent, epoch: "independent-fixture",
    model: { provider: "controlled", id: "test", api: "openai-completions", baseUrl: "http://127.0.0.1:1/v1",
      contextWindow: 131072, maxTokens: 8192 }, thinkingLevel: "off", serviceTierByFamily: {},
    existingContextTokens: 0, outputCap: 8192, reasoningTokens: 0, visibleResultTokens: 8192,
    outputMeaning: "reasoning_and_visible", outputLimitEnforced: true,
    outputLimitSource: "host_default", estimator: "utf8_bytes_upper_bound" };
  (metadata as any).childSelection = {source:"configured_worker_default",modelTableDigest:"a".repeat(64),
    descriptorSource:"manager_model_registry_getAll_all",serviceTierSource:"parent_session_readback",
    model:{...metadata.model,id:"worker"},thinkingLevel:"high",selector:"controlled/worker:high"};
  const request = { schemaVersion: 1, requestId: randomUUID(), subject, revision: null,
    authorizationRef: randomUUID(), authorizationEpoch: 1, skillName: "important-log-summary", skillDigest,
    sources: [span], sourceGaps: [], priorCumulativeEntryId: null,
    inputKey: summaryInputKey(subject as any, null, [span] as any, [], skillDigest),
    dbHealth: { schemaVersion: 1, dbUuid: randomUUID(), measuredAt: "2026-10-12T00:00:00Z", stale: false,
      mainBytes: 0, walBytes: 0, shmBytes: 0, totalBytes: 0, logicalLiveBytes: 0, freelistBytes: 0,
      subjectStats: [], cleanupCandidates: null, lastWrite: null, lastLock: null, lastMaintenance: null,
      diskFreeBytes: null, lastQuickCheck: null, metadataOutsideBudget: true }, requestedAt: "2026-10-12T00:00:00Z" };
  return { role: "manager", context: { model: structuredClone(metadata.model), modelRegistry:{getAll:(mode:string)=>{assert.equal(mode,"all");return [(metadata as any).childSelection.model];}}, sessionManager: { getSessionId: () => parent } },
    prepared: { request, metadata: structuredClone(metadata), selected: [{ range: span, text: "work" }], omitted: [],
      priorCumulative: null, definitionDigest: digest(definition) },
    writer: {}, metadata };
}

async function exercise(stage: "first" | "followup", budget: "input" | "output",
                        safety?: "executed" | "duplicate" | "mismatch") {
  const input = ownedRequest();
  const counters = { dispatch: 0, write: 0, dispose: 0, result: 0, abort: 0 };
  const trace: any[] = [];
  class PrivateRegistry {
    entries = new Map<string, any>();
    get(id: string) { return this.entries.get(id); }
    static global() { return { get() { return undefined; } }; }
  }
  const sdk = { AgentRegistry: PrivateRegistry, Settings: { isolated: (value: any) => value },
    SessionManager: { inMemory: () => ({}) }, Tokenizer: class {},
    toolWireSchema: (tool: any) => tool.parameters,
    getBundledAgent: () => ({ source: "bundled", filePath: "embedded:task.md", systemPrompt: definition }),
    fitOutputTokensToContextWindow: (_model: any, context: any, cap: number) => context.outputRefused ? cap - 1 : cap,
    async createAgentSession(options: any) {
      const listeners: ((event: any) => void)[] = [];
      const id = randomUUID();
      const child: any = { settings: options.settings, model: options.model, thinkingLevel: options.thinkingLevel,
        serviceTierByFamily: options.resolveServiceTierByFamily(), isDisposed: false,
        sessionManager: { getSessionId: () => id }, getEnabledToolNames: () => ["db_summary_save"],
        agent: { streamFn: (_model: any, _context: any, opts: any) => { assert.equal(opts.maxTokens, 8192); counters.dispatch++; } },
        subscribe: (fn: any) => { listeners.push(fn); return () => {}; },
        abort: async () => { counters.abort++; },
        dispose: async () => { counters.dispose++; child.isDisposed = true; },
        async prompt(text: string) {
          const context = (refused: boolean) => ({ systemPrompt: options.systemPrompt,
            // LG-D26: a later turn needs actual wire overflow, not another host quota.
            messages: [{ role: "user", content: refused && budget === "input" ? "x".repeat(32768) : text }],
            tools: [{ name: "db_summary_save", description: options.customTools[0].description,
              parameters: options.customTools[0].parameters }], outputRefused: refused && budget === "output" });
          if (stage === "followup") {
            await child.agent.streamFn(child.model, context(false), {});
            const reply = await options.customTools[0].execute("summary", { facts: ["observed"], interpretations: [], unknowns: [] });
            assert.equal(reply.isError, undefined);
            counters.result++;
          }
          await assert.rejects(async () => child.agent.streamFn(child.model, context(true), {}),
            new RegExp(budget + "_budget_unavailable"));
          // Supported owner callback/event surfaces; no guard code is removed.
          if (safety === "executed") listeners.forEach(fn => fn({ type: "tool_execution_end", toolName: "bash", isError: false }));
          if (safety === "duplicate") {
            const refused = await options.customTools[0].execute("duplicate", { facts: ["again"], interpretations: [], unknowns: [] });
            assert.equal(refused.isError, true);
          }
          if (safety === "mismatch") input.prepared.request.requestId = randomUUID();
        } };
      options.agentRegistry.entries.set(options.agentId, child);
      return { session: child };
    } };
  const result = await dispatchSummarySDK({ ...input, sdk, current: () => true,
    observeMetadata: () => input.metadata, emit: event => trace.push(event),
    save: async () => { counters.write++; return { saved: true }; } });
  assert.equal(counters.write, 0);
  assert.equal(Object.hasOwn(result, "receipt"), false);
  assert.equal(counters.dispose, 1);
  assert.equal(counters.dispatch, stage === "first" ? 0 : 1);
  assert.equal(counters.result, stage === "first" ? 0 : 1);
  assert.equal(result.status, stage === "first" ? "deferred" : "rejected");
  assert.equal(result.sent, stage === "followup");
  assert.equal(result.protected, true); // local refusal is separate from Python global P5.
  assert.ok(trace.some(event => event.event === "stream_guard" && event.reason === budget + "_budget_unavailable"));
  return { result, counters };
}

test("first and later wire/output refusal preserve exact budget cause and write nothing", async () => {
  for (const stage of ["first", "followup"] as const) for (const budget of ["input", "output"] as const) {
    const { result } = await exercise(stage, budget);
    assert.equal(result.reason, budget + "_budget_unavailable");
  }
});

test("confirmed forbidden execution wins over prior budget refusal", async () => {
  for (const budget of ["input", "output"] as const) {
    const { result, counters } = await exercise("followup", budget, "executed");
    assert.equal(result.reason, "compatibility_failed");
    assert.equal(counters.abort, 1);
  }
});

test("duplicate submission or changed request retains safety cause after budget refusal", async () => {
  for (const safety of ["duplicate", "mismatch"] as const) {
    const { result } = await exercise("followup", "input", safety);
    assert.equal(result.reason, safety === "duplicate" ? "guard_violation_observed" : "request_mismatch");
  }
});

async function observedPromptOutcome(providerFailure: boolean) {
  const input=ownedRequest();
  const counters={dispatch:0,write:0,dispose:0};
  class Registry {
    entries=new Map<string,any>();
    get(id:string){return this.entries.get(id);}
    static global(){return {get(){return undefined;}};}
  }
  const sdk={AgentRegistry:Registry,Settings:{isolated:(value:any)=>value},
    SessionManager:{inMemory:()=>({})},Tokenizer:class {},toolWireSchema:(tool:any)=>tool.parameters,
    fitOutputTokensToContextWindow:(_model:any,_context:any,cap:number)=>cap,
    getBundledAgent:()=>({source:"bundled",filePath:"embedded:task.md",systemPrompt:definition}),
    async createAgentSession(options:any){
      const sessionId=randomUUID();
      const child:any={settings:options.settings,model:options.model,thinkingLevel:options.thinkingLevel,
        serviceTierByFamily:options.resolveServiceTierByFamily(),isDisposed:false,
        sessionManager:{getSessionId:()=>sessionId},getEnabledToolNames:()=>["db_summary_save"],
        agent:{streamFn:()=>{counters.dispatch++;}},subscribe:()=>()=>{},abort:async()=>{},
        dispose:async()=>{counters.dispose++;child.isDisposed=true;},
        async prompt(text:string){
          await child.agent.streamFn(child.model,{systemPrompt:options.systemPrompt,
            messages:[{role:"user",content:text}],tools:[{name:"db_summary_save",
              description:options.customTools[0].description,parameters:options.customTools[0].parameters}]},{});
          if (providerFailure) throw new Error("controlled provider connection lost after dispatch");
        }};
      options.agentRegistry.entries.set(options.agentId,child);return {session:child};
    }};
  const outcome=await dispatchSummarySDK({...input,sdk,current:()=>true,observeMetadata:()=>input.metadata,
    save:async()=>{counters.write++;return {saved:true};}});
  assert.equal(outcome.sent,true);assert.equal(outcome.protected,false);
  assert.notEqual(outcome.status,"summary_result");assert.equal(Object.hasOwn(outcome,"receipt"),false);
  assert.deepEqual(counters,{dispatch:1,write:0,dispose:1});
  return {outcome,counters,boundary:"Controlled SDK stream/prompt; actual product dispatchSummarySDK outcome, not installedSDK runtime"};
}

test("provider error shape for Python journal regression", async () => {
  const observed=await observedPromptOutcome(true);
  assert.equal(observed.outcome.status,"unknown");
  assert.equal(observed.outcome.reason,"summary_outcome_unknown");
  const path=process.env.DVL10_REVIEW_PROVIDER_OUTCOME;
  assert.ok(path && path.includes(".workflow/"));
  writeFileSync(path,JSON.stringify(observed,null,2));
});

test("successful empty prompt shape for Python journal regression", async () => {
  const observed=await observedPromptOutcome(false);
  assert.deepEqual(observed.outcome,{status:"rejected",sent:true,protected:false,reason:"result_unavailable"});
  const path=process.env.DVL10_REVIEW_PROVIDER_OUTCOME;
  assert.ok(path && path.includes(".workflow/"));
  writeFileSync(path+"-empty.json",JSON.stringify(observed,null,2));
});

test("confirmed forbidden end with abort rejection shape for Python protection regression", async () => {
  const input=ownedRequest();
  const counters={dispatch:0,write:0,abort:0,dispose:0};
  const trace:any[]=[];
  class Registry {
    entries=new Map<string,any>();
    get(id:string){return this.entries.get(id);}
    static global(){return {get(){return undefined;}};}
  }
  const sdk={AgentRegistry:Registry,Settings:{isolated:(value:any)=>value},
    SessionManager:{inMemory:()=>({})},Tokenizer:class {},toolWireSchema:(tool:any)=>tool.parameters,
    fitOutputTokensToContextWindow:(_model:any,_context:any,cap:number)=>cap,
    getBundledAgent:()=>({source:"bundled",filePath:"embedded:task.md",systemPrompt:definition}),
    async createAgentSession(options:any){
      const listeners:((event:any)=>void)[]=[];
      const sessionId=randomUUID();
      const child:any={settings:options.settings,model:options.model,thinkingLevel:options.thinkingLevel,
        serviceTierByFamily:options.resolveServiceTierByFamily(),isDisposed:false,
        sessionManager:{getSessionId:()=>sessionId},getEnabledToolNames:()=>["db_summary_save"],
        agent:{streamFn:()=>{counters.dispatch++;}},
        subscribe:(fn:any)=>{listeners.push(fn);return ()=>{};},
        abort:async()=>{counters.abort++;throw new Error("controlled SDK abort rejected");},
        dispose:async()=>{counters.dispose++;child.isDisposed=true;},
        async prompt(text:string){
          await child.agent.streamFn(child.model,{systemPrompt:options.systemPrompt,
            messages:[{role:"user",content:text}],tools:[{name:"db_summary_save",
              description:options.customTools[0].description,parameters:options.customTools[0].parameters}]},{});
          // Normal subscribed SDK event surface; no guard is removed or replaced.
          listeners.forEach(fn=>fn({type:"tool_execution_end",toolName:"bash",isError:false}));
        }};
      options.agentRegistry.entries.set(options.agentId,child);return {session:child};
    }};
  const outcome=await dispatchSummarySDK({...input,sdk,current:()=>true,observeMetadata:()=>input.metadata,
    emit:event=>trace.push(event),save:async()=>{counters.write++;return {saved:true};}});
  assert.deepEqual(outcome,{status:"unknown",sent:true,protected:true,reason:"compatibility_failed"});
  assert.deepEqual(counters,{dispatch:1,write:0,abort:1,dispose:1});
  assert.equal(Object.hasOwn(outcome,"receipt"),false);
  assert.ok(trace.some(event=>event.event==="compatibility_failed"));
  const path=process.env.DVL10_PROTECTED_UNKNOWN_OUTCOME;
  assert.ok(path && path.includes(".workflow/"));
  writeFileSync(path,JSON.stringify({outcome,counters,trace,
    boundary:"Actual product dispatchSummarySDK subscriber/catch/finally with controlled SDK event and abort; no installedSDK/POST/tool execution claim"},null,2));
});

const u1Observations:any[]=[];
async function u1Turn(scenario:"first_quota"|"ten_percent"|"continuation"|"mandatory_context"|"mandatory_fit"|"later_id") {
  const input=ownedRequest();
  const text="work "+"x".repeat(scenario==="first_quota"?9000:2600);
  const span=input.prepared.request.sources[0];span.end=Buffer.byteLength(text);span.digest=digest(text);
  input.prepared.selected[0].text=text;
  input.prepared.request.inputKey=summaryInputKey(input.prepared.request.subject as any,null,[span] as any,[],input.prepared.request.skillDigest);
  if(scenario==="ten_percent")(input.metadata as any).childSelection.model.contextWindow=50000;
  if(scenario==="mandatory_context")(input.metadata as any).childSelection.model.contextWindow=24000;
  input.prepared.metadata=structuredClone(input.metadata);
  const counters={create:0,prompt:0,dispatch:0,write:0,dispose:0};const trace:any[]=[];
  class Registry {
    entries=new Map<string,any>();get(id:string){return this.entries.get(id);}
    static global(){return {get(){return undefined;}};}
  }
  const sdk={AgentRegistry:Registry,Settings:{isolated:(value:any)=>value},SessionManager:{inMemory:()=>({})},
    Tokenizer:class {},toolWireSchema:(tool:any)=>tool.parameters,
    fitOutputTokensToContextWindow:(_model:any,ctx:any,cap:number)=>
      scenario==="mandatory_fit"&&ctx.messages?.some((m:any)=>m.role==="toolResult")?cap-1:cap,
    getBundledAgent:()=>({source:"bundled",filePath:"embedded:task.md",systemPrompt:definition}),
    async createAgentSession(options:any){
      counters.create++;const id=randomUUID();const tool=options.customTools[0];
      const child:any={settings:options.settings,model:options.model,thinkingLevel:options.thinkingLevel,
        serviceTierByFamily:options.resolveServiceTierByFamily(),isDisposed:false,
        sessionManager:{getSessionId:()=>id},getEnabledToolNames:()=>["db_summary_save"],
        agent:{streamFn:(_m:any,_c:any,opts:any)=>{assert.equal(opts.maxTokens,8192);counters.dispatch++;}},
        subscribe:()=>()=>{},abort:async()=>{},dispose:async()=>{counters.dispose++;child.isDisposed=true;},
        async prompt(prompt:string){
          counters.prompt++;
          const initial:any={systemPrompt:options.systemPrompt,messages:[{role:"user",content:prompt}],
            tools:[{name:tool.name,description:tool.description,parameters:tool.parameters}]};
          await child.agent.streamFn(child.model,initial,{});
          const params={facts:['한"\\'.repeat(400)],interpretations:[],unknowns:[]};
          assert.ok(Buffer.byteLength(JSON.stringify(params))<8192);
          const result=await tool.execute("observed-save",params);assert.equal(result.isError,undefined);
          const callId=scenario==="later_id"?"provider-extra-"+"i".repeat(24000):"observed-save";
          const next={...initial,messages:[...initial.messages,
            {role:"assistant",content:[{type:"toolCall",id:callId,name:tool.name,arguments:params}],
              api:child.model.api,provider:child.model.provider,model:child.model.id,stopReason:"toolUse",timestamp:1},
            {role:"toolResult",toolCallId:callId,toolName:tool.name,content:result.content,isError:false,timestamp:2}]};
          if(scenario==="later_id")await assert.rejects(async()=>child.agent.streamFn(child.model,next,{}),/input_budget_unavailable/);
          else await child.agent.streamFn(child.model,next,{});
        }};
      options.agentRegistry.entries.set(options.agentId,child);return {session:child};
    }};
  const outcome=await dispatchSummarySDK({...input,sdk,current:()=>true,observeMetadata:()=>input.metadata,
    emit:event=>trace.push(event),save:async()=>{counters.write++;return {saved:true,entryId:randomUUID()};}});
  u1Observations.push({scenario,outcome,counters,trace,modelWindow:input.metadata.model.contextWindow,
    existingContext:input.metadata.existingContextTokens,boundary:"controlledSDK, actual product dispatch; projection not provider payload proof"});
  const path=process.env.DVL10_U1_BUDGET_EVIDENCE;
  if(path){assert.ok(path.includes(".workflow/"));writeFileSync(path,JSON.stringify(u1Observations,null,2));}
  return {outcome,counters,trace,input};
}

test("LGD30 removed first cost quota admits wire-fitting input on selected child",async()=>{
  for(const scenario of ["first_quota","ten_percent"] as const){
    const {outcome,counters,trace,input}=await u1Turn(scenario);
    assert.equal(outcome.status,"summary_result");assert.equal(outcome.sent,true);
    assert.deepEqual(counters,{create:1,prompt:1,dispatch:2,write:1,dispose:1});
    const preflight=trace.find(e=>e.event==="dispatch_preflight");
    assert.equal(preflight.inputLimit,(input.metadata as any).childSelection.model.contextWindow);
    assert.equal(preflight.outputCap,8192);
    assert.ok(preflight.mandatoryWireBytes<=32768);
  }
});

test("LGD26 mandatory full next output and fitted cap are checked before prompt",async()=>{
  for(const scenario of ["mandatory_context","mandatory_fit"] as const){
    const {outcome,counters,trace,input}=await u1Turn(scenario);
    assert.deepEqual(outcome,{status:"deferred",sent:false,protected:false,
      reason:scenario==="mandatory_context"?"input_budget_unavailable":"output_budget_unavailable"});
    assert.equal(counters.prompt,0);assert.equal(counters.dispatch,0);assert.equal(counters.write,0);
    const e=trace.find(e=>e.event==="dispatch_preflight");
    assert.equal(e.previousOutputReserve,8192);assert.equal(e.nextOutputReserve,8192);
    assert.equal(e.mandatoryArgumentsBytes,8192);
    if(scenario==="mandatory_context"){
      assert.ok(e.inputEstimate+8192<=(input.metadata as any).childSelection.model.contextWindow);
      assert.ok(e.inputEstimate+e.fixedContinuationEstimate+16384>(input.metadata as any).childSelection.model.contextWindow);
    }else{assert.equal(e.fittedCap,8192);assert.equal(e.nextFittedCap,8191);}
  }
});

test("LGD26 valid escaped multibyte continuation above host quota can complete",async()=>{
  const {outcome,counters,trace}=await u1Turn("continuation");
  assert.equal(outcome.status,"summary_result");assert.equal(outcome.sent,true);assert.equal(outcome.protected,false);
  assert.deepEqual(counters,{create:1,prompt:1,dispatch:2,write:1,dispose:1});
  const actual=trace.filter(e=>e.event==="dispatch_preflight");
  const next=actual.find(e=>e.initialInvocation===false);
  assert.ok(next.inputEstimate>8192);assert.equal(next.inputLimit,131072);
  assert.ok(next.conservativeWireBytes<=32768);assert.equal(next.fittedCap,8192);
});

test("LGD26 unexpected later provider ID exceeds remaining wire without writing",async()=>{
  const {outcome,counters,trace}=await u1Turn("later_id");
  assert.deepEqual(outcome,{status:"rejected",sent:true,protected:true,reason:"input_budget_unavailable"});
  assert.deepEqual(counters,{create:1,prompt:1,dispatch:1,write:0,dispose:1});
  assert.equal(Object.hasOwn(outcome,"receipt"),false);
  const next=trace.find(e=>e.event==="dispatch_preflight"&&e.initialInvocation===false);
  assert.ok(next.conservativeWireBytes>32768);assert.ok(trace.some(e=>e.event==="stream_guard"&&e.reason==="input_budget_unavailable"));
});
