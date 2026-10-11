import { createHash } from "node:crypto";
import { appendFileSync, readFileSync } from "node:fs";
import { AgentRegistry, createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";

const trace = process.env.Q01_FULL_PREPARE_TRACE!;
const bundled = getBundledAgent("task");
if (!bundled || bundled.source !== "bundled" || bundled.filePath !== "embedded:task.md") {
  throw new Error("bundled_task_definition_unavailable");
}
const body = bundled.systemPrompt;
const fixture = readFileSync(process.env.Q01_AGENT_BODY!, "utf8");
const skill = readFileSync(process.env.Q01_SUMMARY_SKILL!, "utf8");
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
const log = (entry: Record<string, unknown>) => appendFileSync(trace, JSON.stringify({ pid: process.pid, ...entry }) + "\n");

async function prepare(ctx: any) {
  let child: any;
  try {
    const registry = new AgentRegistry();
    const agentId = "q01-full-prepare-task";
    const settings = Settings.isolated({ externalThinking: false, "prewalk.enabled": false,
      "tools.intentTracing": false, "retry.usageAwareFallback": false });
    const saveTool = { name: "db_summary_save", label: "Save summary", description: "Fixed private summary writer.",
      approval: "write" as const, loadMode: "essential" as const,
      parameters: { type: "object", properties: { summary: { type: "string" } },
        required: ["summary"], additionalProperties: false },
      async execute() { throw new Error("prepare_must_not_execute_tool"); } };
    child = (await createAgentSession({
      cwd: process.cwd(), model: ctx.model, settings, sessionManager: SessionManager.inMemory(process.cwd()),
      agentRegistry: registry, agentId, agentName: "task", bindProcessState: false,
      systemPrompt: [body, skill], cacheWarming: false, disableExtensionDiscovery: true,
      preloadedPreparedExtensions: [], enableMCP: false, enableLsp: false, enableIrc: false,
      skipPythonPreflight: true, skills: [], rules: [], contextFiles: [], promptTemplates: [], slashCommands: [],
      toolNames: ["db_summary_save"], restrictToolNames: true, allowRestrictedCustomTools: true,
      requireYieldTool: false, customTools: [saveTool], hasUI: false, autoApprove: true,
      deadline: Date.now() + 10000,
    })).session;
    const model = child.model;
    log({ event: "readback", active: child.getEnabledToolNames(), sessionId: child.sessionManager.getSessionId(),
      settingsIdentity: child.settings === settings, privateHas: Boolean(registry.get(agentId)),
      globalHas: Boolean(AgentRegistry.global().get(agentId)),
      parentEntries: ctx.sessionManager?.getEntries?.().length ?? null,
      bodySha: sha(body), skillSha: sha(skill),
      agentDefinition: { name: bundled.name, source: bundled.source, filePath: bundled.filePath },
      fixtureSha: sha(fixture), fixtureMatches: body === fixture,
      model: model && { provider: model.provider, id: model.id, api: model.api,
        contextWindow: model.contextWindow, maxTokens: model.maxTokens, baseUrl: model.baseUrl },
      thinkingLevel: child.thinkingLevel, serviceTierByFamily: child.serviceTierByFamily });
    log({ event: "done", ok: true });
  } catch (error) {
    log({ event: "done", ok: false, error: String(error) });
  } finally {
    if (child) { await child.dispose(); log({ event: "disposed" }); }
  }
}

export default function fullPrepare(pi: any) {
  pi.on("session_start", (_: unknown, ctx: any) => { void prepare(ctx); });
}
