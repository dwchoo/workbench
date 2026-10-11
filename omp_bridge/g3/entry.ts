/** Installed OMP resolves embedded SDK packages at this extension entry. */
import { AgentRegistry, createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";
import { toolWireSchema } from "@oh-my-pi/pi-ai";
import { Tokenizer, fitOutputTokensToContextWindow } from "@oh-my-pi/pi-agent-core";
import { getBundledAgent } from "@oh-my-pi/pi-coding-agent/task/agents";
import workbenchG3Extension from "./bridge.ts";

export default function workbenchEntry(pi: any): void {
  workbenchG3Extension(pi, { AgentRegistry, createAgentSession, SessionManager, Settings,
    toolWireSchema, Tokenizer, fitOutputTokensToContextWindow, getBundledAgent });
}
