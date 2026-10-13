/** Explicit private one-shot C1 entry. This fixture cannot attest operating MODEL. */
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, realpathSync, statSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { AgentRegistry, createAgentSession, SessionManager, Settings, VERSION } from "@oh-my-pi/pi-coding-agent";
import { toolWireSchema } from "@oh-my-pi/pi-ai";
import { Tokenizer, fitOutputTokensToContextWindow } from "@oh-my-pi/pi-agent-core";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";
import { canonicalJson, summaryInputKey } from "../contract/logs_v1.ts";
import { observeModelMetadata } from "./model_metadata.ts";
import { buildSummaryPrompt, dispatchVerificationSummarySDK } from "./summary_sdk.ts";
import { publishVerificationCompletion } from "./verification_completion.ts";

const sha = (value: string) => createHash("sha256").update(value).digest("hex");
const stable = (value: any) => canonicalJson(value);
const modelFields = ["provider", "id", "api", "baseUrl", "contextWindow", "maxTokens", "tokenizer"];
const model = (value: any) => Object.fromEntries(modelFields.map(key => [key, value?.[key] ?? null]));
let started = false;

export default function verification(pi: any) {
  pi.on("session_start", async (_event: any, context: any) => {
    if (started) return;
    started = true;
    const root = process.env.WORKBENCH_SUMMARY_VERIFY_ROOT;
    // This entry has no operating route: absent ownership fails before any child.
    if (!root) return;
    let owned = false;
    try {
      const info = statSync(root);
      if (realpathSync(root) !== root || (info.mode & 0o777) !== 0o700 || info.uid !== process.getuid?.()
          || realpathSync(process.env.HOME!) !== root + "/home"
          || realpathSync(process.cwd()) !== root + "/project"
          || realpathSync(fileURLToPath(new URL("../", import.meta.url))) !== root + "/runtime/omp_bridge")
        throw Error("unsafe_verification_root");
      owned = true;
      await run(root, context);
    } catch (error) {
      if (owned) publishVerificationCompletion(root, { error: String(error), exercised: [] }, "err");
    }
  });
}

async function run(root: string, ctx: any) {
  const fixture = JSON.parse(readFileSync(root + "/fixture.json", "utf8"));
  const restartPhase = fixture.restartControl === true ? fixture.phase : undefined;
  if (restartPhase !== undefined && !["disconnect", "restart"].includes(restartPhase))
    throw Error("owned_restart_phase_unavailable");
  const endpoint = new URL(fixture.endpoint);
  if (fixture.root !== root || endpoint.protocol !== "http:" || endpoint.hostname !== "127.0.0.1"
      || !endpoint.port || endpoint.pathname !== "/v1" || endpoint.username || endpoint.password
      || endpoint.search || endpoint.hash || ctx.model?.baseUrl !== fixture.endpoint
      || ctx.model?.provider !== "ownedverify" || ctx.model?.id !== "scripted")
    throw Error("owned_model_endpoint_unavailable");
  const parent = AgentRegistry.global().list().find((row: any) =>
    row.session?.sessionManager.getSessionId() === ctx.sessionManager.getSessionId())?.session;
  if (!parent || ctx.modelRegistry !== parent.modelRegistry) throw Error("native_parent_registry_unavailable");
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), 25000);
  try { await ctx.modelRegistry.awaitInitialBackgroundRefresh(abort.signal); }
  finally { clearTimeout(timer); }
  if (abort.signal.aborted) throw Error("preparation_not_settled");
  const state = () => ({ model: model(parent.model), thinkingLevel: parent.thinkingLevel,
    serviceTierByFamily: structuredClone(parent.serviceTierByFamily), entries: parent.sessionManager.getEntries().length });
  const registry = () => {
    const rows = ctx.modelRegistry.getAll("all").map(model).sort((a: any, b: any) => stable(a).localeCompare(stable(b)));
    return { count: rows.length, sha256: sha(stable(rows)) };
  };
  const settings = () => Object.fromEntries((readdirSync(root, { recursive: true }) as string[])
    .filter(name => !name.startsWith("runtime/") && ["config.yml", "config.yaml", "settings.json", "settings.yml",
      "models.yml", "overlay.yml"].includes(name.split("/").at(-1)!))
    .sort().map(name => [name, sha(readFileSync(root + "/" + name, "utf8"))]));
  const before = state(), registryBefore = registry(), settingsBefore = settings();
  const observed = observeModelMetadata(ctx, { AgentRegistry });
  if (!observed) throw Error("public_metadata_unavailable");
  // These are declared semantics of the owned no-reasoning scripted response,
  // empty fixture context and 8192-token cap, never operating getter evidence.
  // Native model/tokenizer fields are kept verbatim; budget refusal stays refusal.
  const metadata = () => {
    const current = observeModelMetadata(ctx, { AgentRegistry });
    return current ? { ...current, existingContextTokens: 0, outputCap: 8192, reasoningTokens: 0,
      visibleResultTokens: 8192, outputMeaning: "reasoning_and_visible", outputLimitEnforced: true,
      outputLimitSource: "owned-scripted-fixture-declaration-not-operating-proof" } : undefined;
  };
  const bundled = getBundledAgent("task");
  if (!bundled) throw Error("bundled_definition_unavailable");
  const skill = readFileSync(fileURLToPath(new URL("../skills/important-log-summary/SKILL.md", import.meta.url)), "utf8");
  const originalText = "소유 private 환경에서 합성 입력의 C1 연결을 확인한다.";
  const originalBytes = Buffer.byteLength(originalText);
  const text = restartPhase === "restart" ? originalText.replace("C1", "C3") : originalText;
  if (Buffer.byteLength(text) !== originalBytes) throw Error("owned_restart_fixture_mismatch");
  const offset = restartPhase === "restart" ? originalBytes : 0;
  const commandId = "cfa0abb4-03c6-4c1f-9366-a7b9d48ac9f7";
  const subject = { schemaVersion: 1, kind: "command", id: commandId };
  const span = { schemaVersion: 1, source: { schemaVersion: 1, sourceKind: "terminal", collectorEpoch: 1, commandId },
    start: offset, end: offset + Buffer.byteLength(text), digest: sha(text) };
  const requestId = restartPhase === "restart" ? "a40f9a18-a0fc-4d75-9260-7f83cb06622d" : "fc8416c2-7d8c-46d4-9615-b833b567f9fe";
  const skillDigest = sha(skill);
  const request = { schemaVersion: 1, requestId, subject, revision: null,
    authorizationRef: "0f3015c7-dae8-4dc3-9194-2cf770b14cf0", authorizationEpoch: 1,
    inputKey: summaryInputKey(subject as any, null, [span] as any, [], skillDigest),
    skillName: "important-log-summary", skillDigest, sources: [span], sourceGaps: [], priorCumulativeEntryId: null,
    dbHealth: fixture.dbHealth, requestedAt: new Date().toISOString() };
  const prepared = { request, metadata: metadata(), selected: [{ range: span, text }], omitted: [],
    priorCumulative: null, definitionDigest: sha(bundled.systemPrompt) };
  const python = process.env.WORKBENCH_SUMMARY_VERIFY_PYTHON!;
  const moduleRoot = root + "/runtime/src";
  const journal = (mode: "--reserve" | "--hold" | "--confirm-not-sent", item: any) => new Promise<void>((resolve, reject) => {
    const reservation = execFile(python, ["-m", "workbench.backend.summary_compat_runtime", mode, root],
      { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH!, PYTHONPATH: moduleRoot,
        HOME: root + "/home", PYTHONDONTWRITEBYTECODE: "1" } }, error => error ? reject(error) : resolve());
    reservation.stdin!.end(JSON.stringify(item));
  });
  const expected = (input: any) => {
    // Match dispatch's detached canonical snapshot without changing its prompt bytes.
    const prompt = buildSummaryPrompt(JSON.parse(canonicalJson(input)));
    writeFileSync(root + "/expected.json", JSON.stringify({ requestId: input.request.requestId,
      systemSha: sha(bundled.systemPrompt + "\n\n" + skill), promptSha256: sha(prompt),
      promptBytes: Buffer.byteLength(prompt, "utf8") }), { mode: 0o600 });
  };
  let child: any, creates = 0, calls = 0;
  const events: any[] = [];
  const sdk = { AgentRegistry, SessionManager, Settings, toolWireSchema, Tokenizer, fitOutputTokensToContextWindow,
    getBundledAgent, async createAgentSession(options: any) {
      if (++creates !== 1 || options.modelRegistry !== ctx.modelRegistry || Object.hasOwn(options, "authStorage"))
        throw Error("owned_one_child_limit");
      const actual = await createAgentSession(options);
      child = actual.session;
      child.subscribe((event: any) => { if (event.type === "tool_execution_start") calls++; });
      events.push({ event: "owned_child_created", sessionId: String(child.sessionManager.getSessionId()),
        nativeRegistrySame: child.modelRegistry === ctx.modelRegistry,
        globalAbsent: !AgentRegistry.global().get(options.agentId), active: child.getEnabledToolNames() });
      return actual;
    } };
  let pauseControl: any;
  if (fixture.pauseControl === true && restartPhase !== "disconnect") {
    const pausedRequest = { ...request, requestId: "96a816b4-a6fd-4d66-a562-c928f6871796" };
    const pausedPrepared = { ...prepared, request: pausedRequest };
    await journal("--hold", pausedRequest);
    expected(pausedPrepared);
    let predicateCalls = 0;
    const paused = await dispatchVerificationSummarySDK({ role: "manager", context: ctx, prepared: pausedPrepared,
      writer: { python, moduleRoot, dataRoot: root + "/data" }, sdk,
      current: () => { predicateCalls++; return false; }, observeMetadata: metadata,
      emit: event => events.push(event) });
    const promptStarts = events.filter(event => event.event === "prompt_start").length;
    if (predicateCalls < 1 || paused.status !== "deferred" || paused.sent !== false || creates !== 0 || promptStarts !== 0)
      throw Error("pause_no_send_control_unobserved");
    // Confirmation follows the observed no-send outcome; hold alone stays prepared.
    await journal("--confirm-not-sent", pausedRequest);
    pauseControl = { control: "owner_predicate_false_control", requestId: pausedRequest.requestId,
      status: paused.status, reason: paused.reason ?? null, sent: paused.sent,
      predicateCalls, childCreates: creates, promptStarts, noSendConfirmed: true,
      metadataSha256: sha(stable(pausedPrepared.metadata)),
      positiveControlSameMetadata: stable(pausedPrepared.metadata) === stable(prepared.metadata) };
  }
  await journal("--reserve", request);
  expected(prepared);
  const outcome = await dispatchVerificationSummarySDK({ role: "manager", context: ctx, prepared,
    writer: { python, moduleRoot, dataRoot: root + "/data" }, sdk, current: () => true,
    observeMetadata: metadata, emit: event => events.push(event) });
  const after = state(), registryAfter = registry(), settingsAfter = settings();
  publishVerificationCompletion(root, { requestId, version: VERSION, outcome, events,
    ...(restartPhase ? { ownedPhase: restartPhase } : {}),
    ...(pauseControl ? { pauseControl } : {}),
    parentBefore: before, parentAfter: after, parentUnchanged: stable(before) === stable(after),
    registryBefore, registryAfter, registryUnchanged: stable(registryBefore) === stable(registryAfter),
    settingsBefore, settingsAfter, settingsUnchanged: stable(settingsBefore) === stable(settingsAfter),
    childDisposed: child?.isDisposed ?? null, childCreates: child ? 1 : 0, toolCalls: calls,
    operatingMetadata: observed, operatingMetadataPass: false,
    fixtureSemantics: "fixed-owned-scripted-only",
    exercised: child ? ["Q01-route", "Q01-tools", "Q01-skill", "Q01-context", "Q01-origin",
      ...(outcome.sent ? ["Q01-result"] : [])] : [] }, "ok");
}
