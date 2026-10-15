/** Deterministic boundary checks; native SDK capability evidence is separate. */
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { summaryInputKey } from "../../omp_bridge/contract/logs_v1.ts";
import { dispatchSummarySDK, dispatchVerificationSummarySDK } from "../../omp_bridge/g3/summary_sdk.ts";
const sha=(text:string)=>createHash("sha256").update(text).digest("hex");
const definition="Existing bundled task definition.";
function fixture() {
 const id=randomUUID(),parentId=randomUUID();
 const subject={schemaVersion:1,kind:"command",id};
 const span={schemaVersion:1,source:{schemaVersion:1,sourceKind:"terminal",collectorEpoch:1,commandId:id},start:0,end:4,digest:sha("work")};
 const skillDigest=sha(readFileSync("omp_bridge/skills/important-log-summary/SKILL.md","utf8"));
 const health={schemaVersion:1,dbUuid:randomUUID(),measuredAt:"2026-10-12T00:00:00Z",stale:false,
  mainBytes:0,walBytes:0,shmBytes:0,totalBytes:0,logicalLiveBytes:0,freelistBytes:0,subjectStats:[],
  cleanupCandidates:null,lastWrite:null,lastLock:null,lastMaintenance:null,diskFreeBytes:null,lastQuickCheck:null,metadataOutsideBudget:true};
 const request={schemaVersion:1,requestId:randomUUID(),subject,revision:null,authorizationRef:randomUUID(),authorizationEpoch:1,
  inputKey:summaryInputKey(subject as any,null,[span] as any,[],skillDigest),skillName:"important-log-summary",skillDigest,
  sources:[span],sourceGaps:[],priorCumulativeEntryId:null,dbHealth:health,requestedAt:"2026-10-12T00:00:00Z"};
 const model={id:"scripted",provider:"fixture",api:"openai-completions",baseUrl:"http://127.0.0.1:1/v1",contextWindow:131072,maxTokens:8192};
 const metadata={source:"parent_session_readback",parentSessionId:parentId,epoch:"readback-1",model,thinkingLevel:"off",
  serviceTierByFamily:{},existingContextTokens:0,outputCap:8192,reasoningTokens:0,visibleResultTokens:8192,
  outputMeaning:"reasoning_and_visible",outputLimitEnforced:true,outputLimitSource:"host_default",estimator:"utf8_bytes_upper_bound"};
 (metadata as any).childSelection={source:"configured_worker_default",modelTableDigest:"a".repeat(64),
  descriptorSource:"manager_model_registry_getAll_all",serviceTierSource:"parent_session_readback",
  model:{...model,id:"worker"},thinkingLevel:"high",selector:"fixture/worker:high"};
 const context:any={model:structuredClone(model),modelRegistry:{getAll:(mode:string)=>{assert.equal(mode,"all");return [(metadata as any).childSelection.model];}},sessionManager:{getSessionId:()=>parentId}};
 return {role:"manager",context,metadata,prepared:{request,metadata:structuredClone(metadata),selected:[{range:span,text:"work"}],omitted:[],priorCumulative:null,definitionDigest:sha(definition)},
  current:()=>true,observeMetadata:()=>metadata,writer:{python:"/fixed/python",moduleRoot:"/fixed/src",dataRoot:"/fixed/data"}};
}
function mockSDK() {
 const created:any[]=[];const activity:string[]=[];
 class Registry {values=new Map();get(id:string){return this.values.get(id);}static global(){return {get:()=>undefined};}}
 const sdk={AgentRegistry:Registry,Settings:{isolated:(values:any)=>values},SessionManager:{inMemory:()=>({})},
  Tokenizer:class{},fitOutputTokensToContextWindow:(_m:any,_c:any,cap:number)=>cap,toolWireSchema:(tool:any)=>tool.parameters,
  getBundledAgent:()=>({name:"task",source:"bundled",filePath:"embedded:task.md",systemPrompt:definition}),
  async createAgentSession(options:any) {
   created.push(options);activity.push("create");const child:any={model:options.model,settings:options.settings,thinkingLevel:options.thinkingLevel,
    serviceTierByFamily:options.resolveServiceTierByFamily(),isDisposed:false,sessionManager:{getSessionId:()=>"mock-child"},getEnabledToolNames:()=>["db_summary_save"],
    agent:{streamFn:()=>activity.push("stream")},subscribe:()=>()=>{},abort:async()=>{},
    async dispose(){child.isDisposed=true;activity.push("dispose");},
    async prompt(text:string){activity.push("prompt");child.agent.streamFn(child.model,{systemPrompt:options.systemPrompt,
     tools:[{name:"db_summary_save",description:options.customTools[0].description,parameters:options.customTools[0].parameters}],messages:[{role:"user",content:text}]},{});
     await options.customTools[0].execute("mock-save",{facts:["work"],interpretations:[],unknowns:[]});}};
   options.agentRegistry.values.set(options.agentId,child);return {session:child};
  }};
 return {sdk,created,activity};
}
const saved=async()=>({saved:true,entryId:randomUUID()});
test("verification waits before create, supplies exact native reference and only child power override",async()=>{
 const input=fixture(),mock=mockSDK();let release!:()=>void;let observedSignal!:AbortSignal;
 const registry={getAll:()=>[(input.metadata as any).childSelection.model],awaitInitialBackgroundRefresh(signal:AbortSignal){observedSignal=signal;mock.activity.push("wait");return new Promise<void>(r=>{release=r;});}};
 input.context.modelRegistry=registry;const before=JSON.stringify(input);
 const pending=dispatchVerificationSummarySDK({...input,sdk:mock.sdk,save:saved});
 assert.equal(mock.created.length,0);assert.deepEqual(mock.activity,["wait"]);assert.equal(observedSignal.aborted,false);
 release();const result=await pending;
 assert.equal(result.status,"summary_result");assert.equal(mock.created.length,1);
 assert.equal(mock.created[0].modelRegistry,registry);assert.equal(mock.created[0].settings["power.sleepPrevention"],"off");
 assert.equal(Object.hasOwn(mock.created[0],"authStorage"),false);assert.equal(JSON.stringify(input),before);
 assert.deepEqual(mock.activity,["wait","create","prompt","stream","dispose"]);
});
test("operating entry resolves configured child registry but ignores verifier-only overrides",async()=>{
 const input=fixture(),mock=mockSDK();let registryReads=0;input.context.modelRegistry={getAll:(mode:string)=>{registryReads++;assert.equal(mode,"all");return [(input.metadata as any).childSelection.model];}};
 const result=await dispatchSummarySDK({...input,sdk:mock.sdk,save:saved,verification:true,modelRegistry:{},powerSleepPrevention:"off"} as any);
 assert.equal(result.status,"summary_result");assert.ok(registryReads>=1);const options=mock.created[0];
 assert.equal(options.modelRegistry,input.context.modelRegistry);assert.equal(Object.hasOwn(options,"authStorage"),false);
 assert.deepEqual(options.settings,{externalThinking:false,"prewalk.enabled":false,"tools.intentTracing":false,"retry.usageAwareFallback":false});
 assert.deepEqual(Object.keys(options).sort(),["cwd","model","modelRegistry","thinkingLevel","resolveServiceTierByFamily","settings","sessionManager","agentRegistry","agentId","agentName","bindProcessState","systemPrompt","cacheWarming","disableExtensionDiscovery","preloadedPreparedExtensions","enableMCP","enableLsp","enableIrc","skipPythonPreflight","skills","rules","contextFiles","promptTemplates","slashCommands","toolNames","restrictToolNames","allowRestrictedCustomTools","requireYieldTool","customTools","hasUI","autoApprove","deadline"].sort());
});
test("missing, throwing getter and rejected public ready API defer without child or prompt",async()=>{
 for(const variant of ["missing","missing_method","getter_throws","method_throws","rejects"]){
  const input=fixture(),mock=mockSDK();
  if(variant==="missing")delete input.context.modelRegistry;
  if(variant==="missing_method")input.context.modelRegistry={};
  if(variant==="getter_throws")Object.defineProperty(input.context,"modelRegistry",{get(){throw Error("unavailable");}});
  if(variant==="method_throws")input.context.modelRegistry={awaitInitialBackgroundRefresh(){throw Error("unavailable");}};
  if(variant==="rejects")input.context.modelRegistry={awaitInitialBackgroundRefresh:()=>Promise.reject(Error("unavailable"))};
  const result=await dispatchVerificationSummarySDK({...input,sdk:mock.sdk,save:saved});
  assert.deepEqual(result,{status:"deferred",sent:false,protected:false,reason:"startup_configuration_unavailable"},variant);
  assert.equal(mock.created.length,0);assert.deepEqual(mock.activity,[]);
 }
});
test("ready wait is bounded even when public method ignores AbortSignal",async t=>{
 t.mock.timers.enable({apis:["setTimeout"]});const input=fixture(),mock=mockSDK();let signal!:AbortSignal;
 input.context.modelRegistry={awaitInitialBackgroundRefresh(s:AbortSignal){signal=s;return new Promise(()=>{});}};
 const pending=dispatchVerificationSummarySDK({...input,sdk:mock.sdk,save:saved});t.mock.timers.tick(25000);
 const result=await pending;assert.equal(signal.aborted,true);
 assert.deepEqual(result,{status:"deferred",sent:false,protected:false,reason:"preparation_not_settled"});assert.equal(mock.created.length,0);
});
test("AbortSignal rejection at timeout remains preparation_not_settled",async t=>{
 t.mock.timers.enable({apis:["setTimeout"]});const input=fixture(),mock=mockSDK();
 input.context.modelRegistry={awaitInitialBackgroundRefresh(signal:AbortSignal){return new Promise((_r,reject)=>signal.addEventListener("abort",()=>reject(Error("aborted"))));}};
 const pending=dispatchVerificationSummarySDK({...input,sdk:mock.sdk,save:saved});t.mock.timers.tick(25000);
 assert.equal((await pending).reason,"preparation_not_settled");assert.equal(mock.created.length,0);
});
test("remote, non-HTTP or credential-bearing model URL is refused before preparation",async()=>{
 for(const url of ["https://api.example.com/v1","http://example.com/v1","http://user:secret@127.0.0.1/v1","http://127.0.0.1/v1?api_key=x","http://localhost.evil/v1"]){
  const input=fixture(),mock=mockSDK();input.context.model.baseUrl=url;
  input.context.modelRegistry={awaitInitialBackgroundRefresh(){throw Error("must not prepare");}};
  const result=await dispatchVerificationSummarySDK({...input,sdk:mock.sdk,save:saved});
  assert.equal(result.reason,"startup_configuration_unavailable",url);assert.equal(result.sent,false);assert.equal(mock.created.length,0);
 }
});
test("canonical request captured before wait rejects owner change without child creation",async()=>{
 const input=fixture(),mock=mockSDK();let release!:()=>void;
 input.context.modelRegistry={awaitInitialBackgroundRefresh(){return new Promise<void>(r=>{release=r;});}};
 const pending=dispatchVerificationSummarySDK({...input,sdk:mock.sdk,save:saved});input.prepared.selected[0].text="changed";release();
 assert.deepEqual(await pending,{status:"deferred",sent:false,protected:true,reason:"request_mismatch"});assert.equal(mock.created.length,0);
});
test("registry reference change during preparation has no guessed fallback",async()=>{
 const input=fixture(),mock=mockSDK();let release!:()=>void;
 input.context.modelRegistry={awaitInitialBackgroundRefresh(){return new Promise<void>(r=>{release=r;});}};
 const pending=dispatchVerificationSummarySDK({...input,sdk:mock.sdk,save:saved});input.context.modelRegistry={};release();
 assert.equal((await pending).reason,"startup_configuration_unavailable");assert.equal(mock.created.length,0);
});
