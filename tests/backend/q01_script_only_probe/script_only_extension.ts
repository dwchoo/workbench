import { execFile } from "node:child_process";
import { appendFileSync } from "node:fs";

const writer = process.env.Q01_FIXED_WRITER;
const python = process.env.Q01_FIXED_PYTHON;
const trace = process.env.Q01_TRACE_PATH;

function record(event: Record<string, unknown>) {
  if (trace) appendFileSync(trace, JSON.stringify(event) + "\n", { encoding: "utf8" });
}

export default function scriptOnlyProbe(pi: any) {
  pi.registerTool({
    name: "db_summary_save",
    label: "Save summary",
    description: "Save one log summary as data to the fixed private SQLite database.",
    approval: "write",
    loadMode: "essential",
    parameters: {
      type: "object",
      properties: { summary: { type: "string", minLength: 1, maxLength: 2000 } },
      required: ["summary"],
      // Preserve unknown properties until execute can reject the entire call.
      additionalProperties: true,
    },
    async execute(id: string, params: Record<string, unknown>) {
      record({ id, event: "received", params });
      if (!params || typeof params !== "object" || Array.isArray(params) ||
          Object.keys(params).length !== 1 || typeof params.summary !== "string" ||
          params.summary.length < 1 || params.summary.length > 2000 || !writer || !python) {
        record({ id, event: "rejected", writerInvoked: false });
        return { content: [{ type: "text", text: "rejected: only summary data accepted" }], isError: true };
      }
      record({ id, event: "writer_invoked", writerInvoked: true,
        argv: [python, writer, JSON.stringify({ summary: params.summary })] });
      const value = await new Promise<{ code: number | null; out: string; err: string }>((resolve) => {
        execFile(python, [writer, JSON.stringify({ summary: params.summary })],
          { timeout: 5000, maxBuffer: 4096, env: { PATH: process.env.PATH || "" } },
          (error, stdout, stderr) => resolve({ code: error ? 1 : 0, out: stdout, err: stderr }));
      });
      return { content: [{ type: "text", text: value.code === 0 ? value.out.trim() : `writer failed: ${value.err.slice(0, 200)}` }],
        isError: value.code !== 0 };
    },
  });
  pi.on("session_start", async () => { await pi.setActiveTools(["db_summary_save"]); });
}
