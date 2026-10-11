/** Probe-local deterministic selection of bounded, source-addressed fixture ranges. */
export type Segment = { source: string; revision: number; kind: "cumulative" | "start_spec" | "decision" | "report" | "terminal";
  start: number; end: number; text: string; sequence: number };

const priority: Record<Segment["kind"], number> =
  { cumulative: 0, start_spec: 1, decision: 2, report: 3, terminal: 4 };
const bytes = (text: string) => Buffer.byteLength(text, "utf8");

/** One owner-side predicate for provisional and final SDK dispatch admission. */
export function budgetAdmission(input: {
  inputBytes: number; inputLimit: number; roundTripAllowance: number;
  contextWindow: number; outputCap: number; fittedCap: number;
  outputMeaningKnown: boolean;
}): { admitted: boolean; reason: "input_budget_unavailable" | "output_budget_unavailable" | null } {
  const { inputBytes, inputLimit, roundTripAllowance, contextWindow, outputCap,
    fittedCap, outputMeaningKnown } = input;
  if (!outputMeaningKnown || !Number.isFinite(outputCap) || outputCap !== 8192 ||
      !Number.isFinite(fittedCap) || fittedCap !== outputCap)
    return { admitted: false, reason: "output_budget_unavailable" };
  if (![inputBytes, inputLimit, roundTripAllowance, contextWindow].every(Number.isFinite) ||
      inputBytes < 0 || inputLimit <= 0 || roundTripAllowance < 0 || contextWindow <= 0 ||
      inputBytes + roundTripAllowance > inputLimit ||
      inputBytes + outputCap + 64 > contextWindow || inputBytes + 8192 > 32768)
    return { admitted: false, reason: "input_budget_unavailable" };
  return { admitted: true, reason: null };
}

export function selectRanges(input: Segment[], maxPayloadBytes: number) {
  const collectedBytes = input.reduce((n, x) => n + bytes(x.text), 0);
  if (collectedBytes > 1048576) throw new Error("collection_over_1MiB");
  const ordered = [...input].sort((a, b) => priority[a.kind] - priority[b.kind] ||
    (a.kind === "cumulative" || a.kind === "terminal" ? b.sequence - a.sequence : a.sequence - b.sequence) ||
    a.source.localeCompare(b.source) || a.start - b.start);
  const selected: Segment[] = [];
  const omitted: Array<{ source: string; revision: number; start: number; end: number; kind: string;
    reason: string; estimatedBytes: number }> = [];
  for (const piece of ordered) {
    if (!Number.isInteger(piece.start) || !Number.isInteger(piece.end) || piece.end < piece.start)
      throw new Error("invalid_source_range");
    const tentative = JSON.stringify({ selected: [...selected, piece] });
    if (bytes(tentative) <= maxPayloadBytes) selected.push(piece);
    else omitted.push({ source: piece.source, revision: piece.revision, start: piece.start,
      end: piece.end, kind: piece.kind, reason: "input_budget", estimatedBytes: bytes(piece.text) });
  }
  // Adjacent source ranges retain their exact source/revision while bounding omission metadata.
  const groups: typeof omitted = [];
  for (const item of omitted.sort((a, b) => a.source.localeCompare(b.source) || a.start - b.start)) {
    const last = groups.at(-1);
    if (last && last.source === item.source && last.revision === item.revision &&
        last.kind === item.kind && last.reason === item.reason && last.end === item.start) {
      last.end = item.end;
      last.estimatedBytes += item.estimatedBytes;
    } else groups.push({ ...item });
  }
  return { collectedBytes, selected, omitted: groups,
    selectedBytes: selected.reduce((n, x) => n + bytes(x.text), 0),
    omittedBytes: groups.reduce((n, x) => n + x.estimatedBytes, 0) };
}
