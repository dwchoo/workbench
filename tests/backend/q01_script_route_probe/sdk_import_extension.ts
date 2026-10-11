import { appendFileSync } from "node:fs";

const tracePath = process.env.Q01_SDK_IMPORT_TRACE;
const specifier = "@oh-my-pi/pi-coding-agent";

function record(event: Record<string, unknown>) {
  if (!tracePath) return;
  appendFileSync(tracePath, JSON.stringify({ pid: process.pid, ...event }) + "\n", "utf8");
}

record({ event: "extension_loaded", specifier });

export default function sdkImportProbe(pi: any) {
  pi.on("session_start", async () => {
    record({ event: "session_start", specifier });
    try {
      const sdk = await import("@oh-my-pi/pi-coding-agent");
      record({ event: "import_result", specifier, ok: true,
        createAgentSessionType: typeof sdk.createAgentSession,
        sessionManagerType: typeof sdk.SessionManager,
        sessionManagerInMemoryType: typeof sdk.SessionManager?.inMemory });
    } catch (error) {
      const err = error as Error;
      record({ event: "import_result", specifier, ok: false,
        errorName: err?.name ?? typeof error,
        errorMessage: String(err?.message ?? error).slice(0, 2000),
        errorStack: String(err?.stack ?? "").slice(0, 4000) });
    }
  });
}
