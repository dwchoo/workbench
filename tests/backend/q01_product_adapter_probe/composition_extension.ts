/** Current product dispatch; fixture authority predicates remain test owner data. */
import {appendFileSync,existsSync,readFileSync,writeFileSync} from "node:fs";
import {createHash} from "node:crypto";
import {AgentRegistry,createAgentSession,SessionManager,Settings} from "@oh-my-pi/pi-coding-agent";
import {toolWireSchema} from "@oh-my-pi/pi-ai";
import {Tokenizer,fitOutputTokensToContextWindow} from "@oh-my-pi/pi-agent-core";
import {getBundledAgent} from "@oh-my-pi/pi-coding-agent/task/agents";
import {dispatchSummarySDK} from "../../../omp_bridge/g3/summary_sdk.ts";
import {observeModelMetadata} from "../../../omp_bridge/g3/model_metadata.ts";
const dir=process.env.Q01_PRODUCT_PRIVATE!;
const log=(row:any)=>appendFileSync(dir+"/trace.jsonl",JSON.stringify({...row,pid:process.pid})+"\n");
const digest=(s:string)=>createHash("sha256").update(s).digest("hex");
export default function probe(pi:any){ pi.on("session_start",(_e:any,ctx:any)=>{
 void run(pi,ctx).catch(e=>log({event:"fatal",error:String(e)})); }); }
async function run(pi:any,ctx:any){
 const parent=AgentRegistry.global().list().find((e:any)=>e.session?.sessionManager.getSessionId()===ctx.sessionManager.getSessionId())?.session;
 if(!parent)throw Error("actual_parent_missing");
 const selection=JSON.parse(process.env.WORKBENCH_G3_SUMMARY_SELECTION??"null");
 await parent.setActiveToolsByName(["read"]);
 const definition=getBundledAgent("task")!;
 const metadata=()=>{const observed=observeModelMetadata(ctx,{AgentRegistry},selection);
 if(!observed)throw Error("actual_parent_model_metadata_unavailable");return observed;};
 writeFileSync(dir+"/metadata.json",JSON.stringify(metadata()));writeFileSync(dir+"/definition.txt",definition.systemPrompt);
 log({event:"ready",metadata:metadata(),definitionDigest:digest(definition.systemPrompt),skillDigest:digest(readFileSync(process.env.Q01_PRODUCT_SKILL!,"utf8")),parentTools:parent.getEnabledToolNames()});
 const deadline=Date.now()+30000;while(!existsSync(dir+"/prepared.json")&&Date.now()<deadline)await new Promise(r=>setTimeout(r,25));
 if(!existsSync(dir+"/prepared.json"))throw Error("prepared_timeout");
 const all=JSON.parse(readFileSync(dir+"/prepared.json","utf8"));
 for(const [name,value]of Object.entries(all)){
  let creates=0;
  const current=()=>{try{return JSON.parse(readFileSync(dir+"/admission.json","utf8"))[name]===true;}catch{return false;}};
  const result=await dispatchSummarySDK({role:"manager",context:ctx,prepared:value,observeMetadata:metadata,current,
   sdk:{AgentRegistry,SessionManager,Settings,toolWireSchema,Tokenizer,fitOutputTokensToContextWindow,getBundledAgent,
    createAgentSession:async(options:any)=>{creates++;const actual=await createAgentSession(options);
     log({event:"actual_created",case:name,childSessionId:actual.session.sessionManager.getSessionId()});return actual;}},
   writer:{python:process.env.Q01_PRODUCT_PYTHON!,moduleRoot:process.env.Q01_PRODUCT_SRC!,dataRoot:dir+"/data"},
   emit:event=>log({...event,case:name})});
  writeFileSync(dir+"/result-"+name+".json",JSON.stringify(result));log({event:"case_done",case:name,creates,status:result.status,sent:result.sent});
  if(process.env.Q01_PRODUCT_STEP==="1"){const limit=Date.now()+8000;while(!existsSync(dir+"/advance-"+name)&&Date.now()<limit)await new Promise(r=>setTimeout(r,10));if(!existsSync(dir+"/advance-"+name))throw Error("owner_advance_timeout");}
 }
 log({event:"done"});
}
