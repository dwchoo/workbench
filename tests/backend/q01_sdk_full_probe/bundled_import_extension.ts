import { createHash } from "node:crypto";
import { appendFileSync, readFileSync } from "node:fs";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";
import existingPrepare from "./prepare_extension";

const sha = (text: string) => createHash("sha256").update(text).digest("hex");

export default function bundledImportProbe(pi: any) {
  const bundled = getBundledAgent("task");
  const fixture = readFileSync(process.env.Q01_AGENT_BODY!, "utf8");
  appendFileSync(process.env.Q01_FULL_PREPARE_TRACE!, JSON.stringify({
    pid: process.pid, event: "bundled_readback", found: Boolean(bundled),
    name: bundled?.name, source: bundled?.source, filePath: bundled?.filePath,
    bodySha: bundled && sha(bundled.systemPrompt),
    bodyUtf8Bytes: bundled && Buffer.byteLength(bundled.systemPrompt, "utf8"),
    fixtureSha: sha(fixture), fixtureMatches: bundled?.systemPrompt === fixture,
  }) + "\n");
  existingPrepare(pi);
}
