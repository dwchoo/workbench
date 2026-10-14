/** Sanitized public getter and already-loaded Manager registry observation. */
import { createHash } from "node:crypto";
import { canonicalJson } from "../contract/logs_v1.ts";

const sha = (value: unknown) => createHash("sha256").update(canonicalJson(value)).digest("hex");
const MODEL_FIELDS = ["id", "provider", "api", "baseUrl", "contextWindow", "maxTokens", "tokenizer"];

/** Public registry exact selection; never provision a model or infer an alias. */
export function resolveSummaryChildModel(context: any, selection: any): any {
  if (selection?.source !== "configured_worker_default"
      || typeof selection.modelTableDigest !== "string" || !/^[0-9a-f]{64}$/.test(selection.modelTableDigest)
      || typeof selection.selector !== "string") return undefined;
  const match = /^([a-z0-9][a-z0-9._-]*)\/([A-Za-z0-9][A-Za-z0-9._-]*):(minimal|low|medium|high|xhigh|max)$/.exec(selection.selector);
  if (!match || typeof context?.modelRegistry?.getAll !== "function") return undefined;
  const rows = context.modelRegistry.getAll("all").filter((model: any) =>
    model.provider === match[1] && model.id === match[2]);
  return rows.length === 1 && sanitizedModel(rows[0]) ? rows[0] : undefined;
}

function sanitizedModel(actual: any): any {
  const model = Object.fromEntries(MODEL_FIELDS.map(key => [key, actual?.[key] ?? null]));
  if (!actual || MODEL_FIELDS.slice(0, 4).some(key => typeof model[key] !== "string" || !model[key])
      || ["contextWindow", "maxTokens"].some(key => !Number.isSafeInteger(model[key]) || model[key] < 1)) return undefined;
  const url = new URL(model.baseUrl);
  if (url.username || url.password || url.search || url.hash) return undefined;
  return model;
}

/** Parent binding and configured child selection share one detached epoch.
 * Provider enforcement/reasoning semantics remain observed unknowns. */
export function observeModelMetadata(context: any, sdk: Record<string, any> | undefined, selection?: any): any {
  const unknowns: string[] = [];
  try {
    const id = context?.sessionManager?.getSessionId?.();
    if (typeof id !== "string" || !id || typeof sdk?.AgentRegistry?.global !== "function") return undefined;
    const matches = sdk.AgentRegistry.global().list().filter((entry: any) =>
      entry.session?.sessionManager?.getSessionId?.() === id);
    if (matches.length !== 1) return undefined;
    const parent = matches[0].session;
    const actual = parent.model;
    const model = sanitizedModel(actual);
    if (!model || parent.modelRegistry !== context.modelRegistry) return undefined;
    const selected = resolveSummaryChildModel(context, selection);
    const childModel = sanitizedModel(selected);
    if (!childModel) return undefined;
    const childSelection = { source: "configured_worker_default", selector: selection.selector,
      modelTableDigest: selection.modelTableDigest, model: childModel,
      descriptorSource: "manager_model_registry_getAll_all",
      thinkingLevel: selection.selector.slice(selection.selector.lastIndexOf(":") + 1),
      serviceTierSource: "parent_session_readback" };
    const live = Object.fromEntries(MODEL_FIELDS.map(key => [key, context.model?.[key] ?? null]));
    if (canonicalJson(live) !== canonicalJson(model)) return undefined;
    const thinking = parent.thinkingLevel;
    const tier = parent.serviceTierByFamily;
    if (typeof thinking !== "string" || thinking.length > 64 || !tier
        || Object.getPrototypeOf(tier) !== Object.prototype
        || Object.entries(tier).some(([key, value]) => key.length > 64
          || typeof value !== "string" || value.length > 64)) return undefined;
    const tiers = JSON.parse(canonicalJson(tier));
    // getAll is the existing public registry seam, not proof of provider semantics.
    // The stats/tokenizer I/O of getContextUsage is not established; do not read it.
    unknowns.push("current_context_unavailable", "output_ceiling_enforcement_unavailable",
      "reasoning_budget_semantics_unavailable");
    const epoch = sha({ parentSessionId: id, model, thinkingLevel: thinking, serviceTierByFamily: tiers, childSelection });
    return { source: "parent_session_readback", parentSessionId: id, epoch,
      model: JSON.parse(canonicalJson(model)), childSelection: JSON.parse(canonicalJson(childSelection)), thinkingLevel: thinking, serviceTierByFamily: tiers,
      role: "manager", inheritanceSource: "existing_manager_public_session_getters",
      existingContextTokens: null, outputCap: Math.min(8192, childModel.maxTokens), reasoningTokens: null, visibleResultTokens: null,
      outputMeaning: "unknown", outputLimitEnforced: null, outputLimitSource: "host_default",
      estimator: "utf8_bytes_upper_bound", status: "inconclusive", unknowns };
  } catch { return undefined; }
}

/** A supplier closure reads the latest context at each count/dispatch boundary. */
export function modelMetadataObserver(context: () => any, sdk: Record<string, any> | undefined, selection?: any): () => any {
  return () => observeModelMetadata(context(), sdk, selection);
}
