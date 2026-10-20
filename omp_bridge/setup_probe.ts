/** Setup-only SDK load proof. No prompt, login, session persistence or model call. */
import { AgentRegistry, createAgentSession, SessionManager, Settings, VERSION } from "@oh-my-pi/pi-coding-agent";
import { toolWireSchema } from "@oh-my-pi/pi-ai";
import { Tokenizer, fitOutputTokensToContextWindow } from "@oh-my-pi/pi-agent-core";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";

export default function setupProbe(pi: any) {
  pi.on("session_start", () => {
    const nonce = process.env.WORKBENCH_SETUP_NONCE;
    if (!nonce || !/^[a-f0-9]{32}$/.test(nonce)) return;
    const definition = getBundledAgent("task");
    const sdk = [AgentRegistry, AgentRegistry.global, createAgentSession, SessionManager.inMemory,
      Settings.isolated, toolWireSchema, Tokenizer, fitOutputTokensToContextWindow, getBundledAgent]
      .every(value => typeof value === "function") && typeof definition?.systemPrompt === "string";
    process.stdout.write(JSON.stringify({ type: "workbench_setup_ready", nonce, version: VERSION, sdk }) + "\n");
  });
}
