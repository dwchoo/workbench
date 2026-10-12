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
    outputLimitSource: "controlled-not-operating-provider-proof", estimator: "utf8_bytes_upper_bound" };
  const request = { schemaVersion: 1, requestId: randomUUID(), subject, revision: null,
    authorizationRef: randomUUID(), authorizationEpoch: 1, skillName: "important-log-summary", skillDigest,
    sources: [span], sourceGaps: [], priorCumulativeEntryId: null,
    inputKey: summaryInputKey(subject as any, null, [span] as any, [], skillDigest),
    dbHealth: { schemaVersion: 1, dbUuid: randomUUID(), measuredAt: "2026-10-12T00:00:00Z", stale: false,
      mainBytes: 0, walBytes: 0, shmBytes: 0, totalBytes: 0, logicalLiveBytes: 0, freelistBytes: 0,
      subjectStats: [], cleanupCandidates: null, lastWrite: null, lastLock: null, lastMaintenance: null,
      diskFreeBytes: null, lastQuickCheck: null, metadataOutsideBudget: true }, requestedAt: "2026-10-12T00:00:00Z" };
  return { role: "manager", context: { model: metadata.model, sessionManager: { getSessionId: () => parent } },
    prepared: { request, metadata, selected: [{ range: span, text: "work" }], omitted: [],
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
            messages: [{ role: "user", content: refused && budget === "input" ? "x".repeat(8193) : text }],
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

test("first and mandatory-followup input/output refusal preserve exact budget cause and write nothing", async () => {
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
