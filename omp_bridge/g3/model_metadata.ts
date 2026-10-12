/** Sanitized public getter observation, never a provider/catalog lookup. */
import { createHash } from "node:crypto";
import { canonicalJson } from "../contract/logs_v1.ts";

const sha = (value: unknown) => createHash("sha256").update(canonicalJson(value)).digest("hex");
const MODEL_FIELDS = ["id", "provider", "api", "baseUrl", "contextWindow", "maxTokens", "tokenizer"];

/** The owner is the existing parent session; no new session is created here.
 * Missing output enforcement/reasoning semantics remain unknown and block the
 * adapter. Model maxTokens alone is not proof of an enforced output ceiling.
 */
export function observeModelMetadata(context: any, sdk: Record<string, any> | undefined): any {
  const unknowns: string[] = [];
  try {
    const id = context?.sessionManager?.getSessionId?.();
    if (typeof id !== "string" || !id || typeof sdk?.AgentRegistry?.global !== "function") return undefined;
    const matches = sdk.AgentRegistry.global().list().filter((entry: any) =>
      entry.session?.sessionManager?.getSessionId?.() === id);
    if (matches.length !== 1) return undefined;
    const parent = matches[0].session;
    const actual = parent.model;
    const model = Object.fromEntries(MODEL_FIELDS.map(key => [key, actual?.[key] ?? null]));
    if (!actual || MODEL_FIELDS.slice(0, 4).some(key => typeof model[key] !== "string" || !model[key])
        || ["contextWindow", "maxTokens"].some(key => !Number.isSafeInteger(model[key]) || model[key] < 1)) return undefined;
    // Credentials/userinfo/query are never copied from a model descriptor.
    const url = new URL(model.baseUrl);
    if (url.username || url.password || url.search || url.hash) return undefined;
    const live = Object.fromEntries(MODEL_FIELDS.map(key => [key, context.model?.[key] ?? null]));
    if (canonicalJson(live) !== canonicalJson(model)) return undefined;
    const thinking = parent.thinkingLevel;
    const tier = parent.serviceTierByFamily;
    if (typeof thinking !== "string" || thinking.length > 64 || !tier
        || Object.getPrototypeOf(tier) !== Object.prototype
        || Object.entries(tier).some(([key, value]) => key.length > 64
          || typeof value !== "string" || value.length > 64)) return undefined;
    const tiers = JSON.parse(canonicalJson(tier));
    // Only field getters with established pure reads are observed. The internal
    // stats/tokenizer I/O of getContextUsage is not established by this seam.
    unknowns.push("current_context_unavailable", "output_ceiling_enforcement_unavailable",
      "reasoning_budget_semantics_unavailable");
    const epoch = sha({ parentSessionId: id, model, thinkingLevel: thinking, serviceTierByFamily: tiers });
    return { source: "parent_session_readback", parentSessionId: id, epoch,
      model: JSON.parse(canonicalJson(model)), thinkingLevel: thinking, serviceTierByFamily: tiers,
      role: "manager", inheritanceSource: "existing_manager_public_session_getters",
      existingContextTokens: null, outputCap: null, reasoningTokens: null, visibleResultTokens: null,
      outputMeaning: "unknown", outputLimitEnforced: null, outputLimitSource: "unavailable",
      estimator: "utf8_bytes_upper_bound", status: "inconclusive", unknowns };
  } catch { return undefined; }
}

/** A supplier closure reads the latest context at each count/dispatch boundary. */
export function modelMetadataObserver(context: () => any, sdk: Record<string, any> | undefined): () => any {
  return () => observeModelMetadata(context(), sdk);
}
