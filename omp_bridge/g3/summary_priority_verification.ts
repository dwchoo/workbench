/** Explicit owned priority verifier; scripted capability, never operating MODEL proof. */
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, realpathSync, renameSync, statSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { AgentRegistry, createAgentSession, SessionManager, Settings, VERSION } from "@oh-my-pi/pi-coding-agent";
import { toolWireSchema } from "@oh-my-pi/pi-ai";
import { Tokenizer, fitOutputTokensToContextWindow } from "@oh-my-pi/pi-agent-core";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";
import { observeModelMetadata } from "./model_metadata.ts";
import { canonicalJson } from "../contract/logs_v1.ts";
import { buildSummaryPrompt, dispatchVerificationSummarySDK } from "./summary_sdk.ts";
import { publishVerificationCompletion } from "./verification_completion.ts";
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
export function observeNativeEntries(entries: any[]) {
  let markerSeen = false;
  const allEntries = entries.map(entry => {
    const s = JSON.stringify(entry);
    if (typeof s !== "string") throw Error("native_entry_JSON_string_unavailable");
    const entryMarkerSeen = s.includes("private-work-");
    markerSeen = markerSeen || entryMarkerSeen;
    return { type: entry.type ?? null, id: entry.id ?? null, parentId: entry.parentId ?? null,
      native_JSON_serialized_entry_sha256: sha(s), bytes: Buffer.byteLength(s) };
  });
  return { entries: entries.length, allEntries, rawUpperMarkerSeen: markerSeen,
    entryView: "native JSON.stringify view; native memory/storage serialization equality unknown; native insertion order, sameprocess before/after only",
    entryViewLimits: "JSON standard undefined properties omitted, array undefined null, toJSON applied; no canonical/storage identity claim" };
}
const fields = ["provider", "id", "api", "baseUrl", "contextWindow", "maxTokens", "tokenizer"];
export default function observer(pi: any) {
  let started = false, owned = false, parent: any, record: any, snapshot: (() => any) | undefined;
  let stopping = false;
  const root = process.env.WORKBENCH_SUMMARY_VERIFY_ROOT!;
  const publish = (kind: string) => {
    const serialized = JSON.stringify(record);
    const tmp = root + "/.ownerready." + kind + ".tmp";
    writeFileSync(tmp, serialized, { flag: "wx", mode: 0o600 });
    renameSync(tmp, root + "/ownerready.json");
  };
  pi.on("session_start", (_event: any, ctx: any) => {
    if (started) return;
    started = true;
    void (async () => {
      const info = statSync(root);
      if (realpathSync(root) !== root || info.uid !== process.getuid?.() || (info.mode & 0o777) !== 0o700
          || realpathSync(process.env.HOME!) !== root + "/home"
          || realpathSync(process.cwd()) !== root + "/project"
          || realpathSync(fileURLToPath(new URL("../", import.meta.url))) !== root + "/runtime/omp_bridge")
        throw Error("unsafe_owned_root");
      owned = true;
      const fixture = JSON.parse(readFileSync(root + "/fixture.json", "utf8"));
      const url = new URL(fixture.endpoint);
      if (fixture.root !== root || url.protocol !== "http:" || url.hostname !== "127.0.0.1"
          || !url.port || url.pathname !== "/v1" || url.username || url.password || url.search || url.hash
          || ctx.model?.baseUrl !== fixture.endpoint || ctx.model?.provider !== "ownedverify" || ctx.model?.id !== "scripted")
        throw Error("owned_endpoint_unavailable");
      const matches = AgentRegistry.global().list().filter((row: any) =>
        row.session?.sessionManager.getSessionId() === ctx.sessionManager.getSessionId());
      if (matches.length !== 1 || ctx.modelRegistry !== matches[0].session.modelRegistry)
        throw Error("native_parent_registry_unavailable");
      parent = matches[0].session;
      let ownedStopRequested = false;
      const ownedStopTimer = ctx.setInterval(() => {
        if (ownedStopRequested || !existsSync(root + "/owned-stop.json")) return;
        ownedStopRequested = true;
        ctx.clearTimer(ownedStopTimer);
        ctx.shutdown();
      }, 20);
      const abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), 25000);
      try { await ctx.modelRegistry.awaitInitialBackgroundRefresh(abort.signal); }
      finally { clearTimeout(timer); }
      if (abort.signal.aborted) throw Error("preparation_not_settled");
      if (ctx.model?.tokenizer !== "claude-v3" || parent.model?.tokenizer !== "claude-v3")
        throw Error("configured_tokenizer_not_read_back");
      snapshot = () => {
        const entries = parent.sessionManager.getEntries();
        const entryView = observeNativeEntries(entries);
        return {
          model: Object.fromEntries(fields.map(key => [key, parent.model?.[key] ?? null])),
          thinkingLevel: parent.thinkingLevel, serviceTierByFamily: structuredClone(parent.serviceTierByFamily),
          ...entryView,
          roster: AgentRegistry.global().list().map((row: any) => String(row.session?.sessionManager.getSessionId())).sort(),
          registry: sha(canonicalJson(parent.modelRegistry.getAll("all").map((item: any) =>
            Object.fromEntries(fields.map(key => [key, item?.[key] ?? null]))).sort((a: any, b: any) => canonicalJson(a).localeCompare(canonicalJson(b))))),
          settings: Object.fromEntries((readdirSync(root, { recursive: true }) as string[])
            .filter(name => !name.startsWith("runtime/") && ["config.yml", "config.yaml", "settings.json", "settings.yml", "models.yml", "overlay.yml"].includes(name.split("/").at(-1)!))
            .sort().map(name => [name, sha(readFileSync(root + "/" + name, "utf8"))]))
        };
      };
      if (process.env.WORKBENCH_G3_ROLE !== "manager") {
        record = { label: "owned_product_priority_capability",
          parent: { parentSessionId: String(parent.sessionManager.getSessionId()),
            model: Object.fromEntries(fields.map(key => [key, parent.model?.[key] ?? null])) },
          before: snapshot(), nativeRegistrySame: true };
        publish("ready");
        return;
      }
      const selection = JSON.parse(process.env.WORKBENCH_G3_SUMMARY_SELECTION ?? "null");
      const metadata = observeModelMetadata(ctx, { AgentRegistry }, selection);
      const definition = getBundledAgent("task")?.systemPrompt;
      if (!metadata || typeof definition !== "string" || !definition) throw Error("native_definition_metadata_unavailable");
      record = { label: "owned_product_priority_capability", metadata, definition,
        definitionBytes: Buffer.byteLength(definition), definitionSha256: sha(definition),
        helperSha256: sha(readFileSync(fileURLToPath(import.meta.url), "utf8")),
        before: snapshot(), nativeRegistrySame: true };
      publish("ready");
      const preparedPath = root + "/prepared.json";
      let preparedText: string | undefined;
      while (!stopping) {
        try { preparedText = readFileSync(preparedPath, "utf8"); break; }
        catch (error: any) { if (error.code !== "ENOENT") throw error; }
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      if (stopping || !preparedText) return;
      const preparedInfo = statSync(preparedPath);
      if (realpathSync(preparedPath) !== preparedPath || preparedInfo.uid !== process.getuid?.()
          || (preparedInfo.mode & 0o777) !== 0o600 || preparedInfo.size > 131072)
        throw Error("unsafe_owned_prepared_file");
      const prepared = JSON.parse(preparedText);
      const liveMetadata = () => observeModelMetadata(ctx, { AgentRegistry }, selection);
      if (prepared.definitionDigest !== sha(definition) || prepared.metadata.epoch !== liveMetadata()?.epoch)
        throw Error("owned_prepared_binding_mismatch");
      const skill = readFileSync(fileURLToPath(new URL("../skills/important-log-summary/SKILL.md", import.meta.url)), "utf8");
      const prompt = buildSummaryPrompt(JSON.parse(canonicalJson(prepared)));
      writeFileSync(root + "/expected.json", JSON.stringify({ requestId: prepared.request.requestId,
        systemSha: sha(definition + "\n\n" + skill), promptSha256: sha(prompt), promptBytes: Buffer.byteLength(prompt) }),
        { flag: "wx", mode: 0o600 });
      let child: any, creates = 0, calls = 0;
      const events: any[] = [];
      const sdk = { AgentRegistry, SessionManager, Settings, toolWireSchema, Tokenizer,
        fitOutputTokensToContextWindow, getBundledAgent, async createAgentSession(options: any) {
          if (++creates !== 1 || options.modelRegistry !== ctx.modelRegistry || Object.hasOwn(options, "authStorage"))
            throw Error("owned_one_child_limit");
          const actual = await createAgentSession(options);
          child = actual.session;
          child.subscribe((event: any) => { if (event.type === "tool_execution_start") calls++; });
          events.push({ event: "owned_child_created", sessionId: String(child.sessionManager.getSessionId()),
            active: child.getEnabledToolNames(), nativeRegistrySame: child.modelRegistry === ctx.modelRegistry,
            globalAbsent: !AgentRegistry.global().get(options.agentId) });
          return actual;
        } };
      const outcome = await dispatchVerificationSummarySDK({ role: "manager", context: ctx, prepared,
        writer: { python: process.env.WORKBENCH_SUMMARY_VERIFY_PYTHON!, moduleRoot: root + "/runtime/src", dataRoot: root + "/data" },
        sdk, current: () => !stopping, observeMetadata: liveMetadata, emit: event => events.push(event) });
      publishVerificationCompletion(root, { outcome, events, version: VERSION, childCreates: child ? 1 : 0,
        childDisposed: child?.isDisposed ?? null, toolCalls: calls, requestId: prepared.request.requestId,
        fixtureSemantics: "fixed-owned-upper-count-and-publicCore-notices; not-operating-proof", operatingMetadataPass: false }, "ok");
    })().catch(error => {
      if (owned) {
        if (!record) { record = { error: String(error) }; publish("err"); }
        else publishVerificationCompletion(root, { error: String(error), exercised: [] }, "err");
      }
    });
  });
  pi.on("session_shutdown", () => {
    stopping = true;
    if (!owned || !record || !snapshot) return;
    try { record.after = snapshot(); }
    catch (error) { record.afterError = String(error); }
    record.afterSource = "session_shutdown_hook readback; not exact count-return instant";
    publish("shutdown");
  });
}
