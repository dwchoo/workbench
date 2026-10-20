/** Real product entry plus the existing credential-free child/writer fixture. */
import { appendFileSync } from "node:fs";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";
import entry from "../../../../../omp_bridge/g3/entry.ts";
import childProbe from "../../../q01_product_adapter_probe/composition_extension.ts";
export default function probe(pi: any) {
  entry(pi);
  const task = getBundledAgent("task");
  if (!task || task.source !== "bundled" || task.filePath !== "embedded:task.md")
    throw new Error("bundled_task_unavailable");
  appendFileSync(process.env.Q01_PRODUCT_PRIVATE! + "/entry.jsonl", JSON.stringify({
    event: "real_entry_registered", name: task.name, source: task.source,
    executablePath: process.execPath, bunVersion: process.versions.bun,
    filePath: task.filePath, systemPromptBytes: Buffer.byteLength(task.systemPrompt),
  }) + "\n");
  childProbe(pi);
}
