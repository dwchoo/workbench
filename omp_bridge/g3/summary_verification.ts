/** Explicit private one-shot C1 entry. This fixture cannot attest operating MODEL. */
import { execFile, execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, realpathSync, statSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { AgentRegistry, createAgentSession, SessionManager, Settings, VERSION } from "@oh-my-pi/pi-coding-agent";
import { toolWireSchema } from "@oh-my-pi/pi-ai";
import { Tokenizer, fitOutputTokensToContextWindow } from "@oh-my-pi/pi-agent-core";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";
import { canonicalJson, summaryInputKey } from "../contract/logs_v1.ts";
import { observeModelMetadata } from "./model_metadata.ts";
import { buildSummaryPrompt, dispatchVerificationSummarySDK, fixedSummaryWriter } from "./summary_sdk.ts";
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
      // TUI starts the initial registry refresh after session_start handlers return.
      void run(root, context).catch(error =>
        publishVerificationCompletion(root, { error: String(error), exercised: [] }, "err"));
    } catch (error) {
      if (owned) publishVerificationCompletion(root, { error: String(error), exercised: [] }, "err");
    }
  });
}

async function run(root: string, ctx: any) {
  const fixture = JSON.parse(readFileSync(root + "/fixture.json", "utf8"));
  if (fixture.metadataOnly === true) {
    const parent = AgentRegistry.global().list().find((row: any) =>
      row.session?.sessionManager.getSessionId() === ctx.sessionManager.getSessionId())?.session;
    if (fixture.root !== root || !parent || parent.modelRegistry !== ctx.modelRegistry)
      throw Error("configured_parent_unavailable");
    const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 25000);
    try { await ctx.modelRegistry.awaitInitialBackgroundRefresh(abort.signal); }
    finally { clearTimeout(timer); }
    if (abort.signal.aborted) throw Error("configured_registry_unavailable");
    const metadata = observeModelMetadata(ctx, { AgentRegistry }, JSON.parse(process.env.WORKBENCH_G3_SUMMARY_SELECTION!));
    const definition = getBundledAgent("task")?.systemPrompt;
    if (!metadata || !definition) throw Error("configured_metadata_unavailable");
    publishVerificationCompletion(root, { metadata, version: VERSION, definitionDigest: sha(definition),
      metadataOnly: true, promptSubmissions: 0 }, "ok");
    return;
  }
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
    serviceTierByFamily: structuredClone(parent.serviceTierByFamily), entries: parent.sessionManager.getEntries().length,
    entriesSha256: sha(JSON.stringify(parent.sessionManager.getEntries())) });
  const registry = () => {
    const rows = ctx.modelRegistry.getAll("all").map(model).sort((a: any, b: any) => stable(a).localeCompare(stable(b)));
    return { count: rows.length, sha256: sha(stable(rows)) };
  };
  const settings = () => Object.fromEntries((readdirSync(root, { recursive: true }) as string[])
    .filter(name => !name.startsWith("runtime/") && ["config.yml", "config.yaml", "settings.json", "settings.yml",
      "models.yml", "overlay.yml"].includes(name.split("/").at(-1)!))
    .sort().map(name => [name, sha(readFileSync(root + "/" + name, "utf8"))]));
  const before = state(), registryBefore = registry(), settingsBefore = settings();
  const selection = JSON.parse(process.env.WORKBENCH_G3_SUMMARY_SELECTION ?? "null");
  const observed = observeModelMetadata(ctx, { AgentRegistry }, selection);
  if (!observed) throw Error("public_metadata_unavailable");
  const metadata = () => observeModelMetadata(ctx, { AgentRegistry }, selection);
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
  const primaryName = restartPhase === "disconnect" ? "disconnect" : "restart";
  const request = fixture.caseControls?.cases[primaryName]?.request ?? { schemaVersion: 1, requestId, subject, revision: null,
    authorizationRef: "0f3015c7-dae8-4dc3-9194-2cf770b14cf0", authorizationEpoch: 1,
    inputKey: summaryInputKey(subject as any, null, [span] as any, [], skillDigest),
    skillName: "important-log-summary", skillDigest, sources: [span], sourceGaps: [], priorCumulativeEntryId: null,
    dbHealth: fixture.dbHealth, requestedAt: new Date().toISOString() };
  const prepared = { request, metadata: metadata(), selected: [{ range: request.sources[0],
    text: fixture.caseControls?.cases[primaryName]?.text ?? text }], omitted: [],
    priorCumulative: null, definitionDigest: sha(bundled.systemPrompt) };
  const python = process.env.WORKBENCH_SUMMARY_VERIFY_PYTHON!;
  const moduleRoot = root + "/runtime/src";
  const ownerEnvironment = { PATH: process.env.PATH!, PYTHONPATH: moduleRoot,
    HOME: root + "/home", PYTHONDONTWRITEBYTECODE: "1" };
  const current = (item: any) => !fixture.caseControls || execFileSync(python,
    ["-m", "workbench.backend.summary_compat_cases", "--current", root],
    { timeout: 5000, maxBuffer: 4096, env: ownerEnvironment, input: JSON.stringify(item) }).toString() === "true";
  const journal = (mode: "--reserve" | "--hold" | "--confirm-not-sent", item: any) => new Promise<void>((resolve, reject) => {
    const reservation = execFile(python, ["-m", "workbench.backend.summary_compat_runtime", mode, root],
      { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH!, PYTHONPATH: moduleRoot,
        HOME: root + "/home", PYTHONDONTWRITEBYTECODE: "1" } }, error => error ? reject(error) : resolve());
    reservation.stdin!.end(JSON.stringify(item));
  });
  const expected = (input: any, name?: string) => {
    // Match dispatch's detached canonical snapshot without changing its prompt bytes.
    const prompt = buildSummaryPrompt(JSON.parse(canonicalJson(input)));
    writeFileSync(root + "/expected.json", JSON.stringify({ requestId: input.request.requestId,
      systemSha: sha(bundled.systemPrompt + "\n\n" + skill), promptSha256: sha(prompt),
      promptBytes: Buffer.byteLength(prompt, "utf8"), ...(name ? { case: name } : {}) }), { mode: 0o600 });
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
  expected(prepared, fixture.caseControls ? primaryName : undefined);
  const outcome = await dispatchVerificationSummarySDK({ role: "manager", context: ctx, prepared,
    writer: { python, moduleRoot, dataRoot: root + "/data" }, sdk, current: () => current(request),
    observeMetadata: metadata, emit: event => events.push(event) });
  const caseControls: any[] = [];
  if (fixture.caseControls && restartPhase !== "disconnect") {
    for (const name of ["normal2", "normal3", "worker", "roster_rebuild", "api_invalid", "stale_invalid", "saved_then_bash"]) {
      const owned = fixture.caseControls.cases[name];
      const input = { ...prepared, request: owned.request, metadata: metadata(),
        selected: [{ range: owned.request.sources[0], text: owned.text }] };
      const prior = state(), registryPrior = registry(), settingsPrior = settings();
      const children: any[] = [], caseEvents: any[] = [];
      let childCreates = 0, writerCalls = 0, disposalBeforeRebuild = true;
      const native = { ...sdk, async createAgentSession(options: any) {
        disposalBeforeRebuild &&= children.every(child => child.isDisposed === true);
        childCreates++;
        if (childCreates > 2 || options.modelRegistry !== ctx.modelRegistry || Object.hasOwn(options, "authStorage"))
          throw Error("owned_case_child_limit");
        const created = await createAgentSession(options);
        const child = created.session;
        children.push(child);
        if (name === "roster_rebuild" && childCreates === 1) child.getEnabledToolNames = () => ["bash"];
        if (name === "api_invalid") child.agent.streamFn = undefined as any;
        if (name === "stale_invalid") {
          const id = String(child.sessionManager.getSessionId()); let read = 0;
          child.sessionManager.getSessionId = () => id + ":stale:" + (++read);
        }
        return created;
      } };
      await journal("--reserve", owned.request);
      expected(input, name);
      const answer = await dispatchVerificationSummarySDK({ role: name === "worker" ? "worker" : "manager",
        context: ctx, prepared: input, writer: { python, moduleRoot, dataRoot: root + "/data" }, sdk: native,
        save: async (...args: Parameters<typeof fixedSummaryWriter>) => { writerCalls++; return fixedSummaryWriter(...args); },
        current: () => current(owned.request), observeMetadata: metadata, emit: event => caseEvents.push(event) });
      execFileSync(python, ["-m", "workbench.backend.summary_compat_cases", "--disposition", root],
        { timeout: 5000, maxBuffer: 4096, env: ownerEnvironment, input: JSON.stringify({ request: owned.request, outcome: answer }) });
      caseControls.push({ name, requestId: owned.request.requestId, outcome: answer, events: caseEvents,
        childCreates, writerCalls, childDisposed: children.every(child => child.isDisposed === true), disposalBeforeRebuild,
        parentUnchanged: stable(prior) === stable(state()), registryUnchanged: stable(registryPrior) === stable(registry()),
        settingsUnchanged: stable(settingsPrior) === stable(settings()),
        receiptBytes: answer.receipt ? Buffer.byteLength(stable(answer.receipt)) : 0 });
    }
  }
  const after = state(), registryAfter = registry(), settingsAfter = settings();
  publishVerificationCompletion(root, { requestId, version: VERSION, definitionDigest: sha(bundled.systemPrompt), outcome, events,
    ...(fixture.caseControls ? { caseControls } : {}),
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
