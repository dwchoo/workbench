// DVL-07: the actual extension publishes a read-only tool to both roles.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import extension from "../../omp_bridge/g3/bridge.ts";

type Frame = Record<string, any>;

test("manager and worker send a bounded read-only workbench_logs request over the bridge", async () => {
	for (const role of ["manager", "worker"] as const) {
		const directory = await mkdtemp(join(tmpdir(), "dvl07-bridge-"));
		const socketPath = join(directory, "bridge.sock");
		const sockets: net.Socket[] = [];
		const seen: Frame[] = [];
		const server = net.createServer(socket => {
			sockets.push(socket);
			let pending = "";
			socket.on("data", chunk => {
				pending += chunk.toString("utf8");
				while (pending.includes("\n")) {
					const index = pending.indexOf("\n");
					const frame = JSON.parse(pending.slice(0, index)) as Frame;
					pending = pending.slice(index + 1);
					seen.push(frame);
					if (frame.kind === "tool_request") {
						socket.write(JSON.stringify({ kind: "tool_result", requestId: frame.requestId,
							toolCallId: frame.toolCallId, result: { status: "ok", data: { complete: true,
								databaseCount: 1 } } }) + "\n");
					}
				}
			});
		});
		const names = ["WORKBENCH_G3_BRIDGE_SOCKET", "WORKBENCH_G3_ROLE", "WORKBENCH_G3_TOKEN",
			"WORKBENCH_G3_GENERATION"];
		const previous = new Map(names.map(name => [name, process.env[name]]));
		const handlers = new Map<string, (...args: any[]) => any>();
		const tools = new Map<string, Frame>();
		try {
			await new Promise<void>((resolve, reject) => {
				server.once("error", reject);
				server.listen(socketPath, resolve);
			});
			process.env.WORKBENCH_G3_BRIDGE_SOCKET = socketPath;
			process.env.WORKBENCH_G3_ROLE = role;
			process.env.WORKBENCH_G3_TOKEN = randomUUID();
			process.env.WORKBENCH_G3_GENERATION = "1";
			const pi = {
				on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
				registerTool(tool: Frame) { tools.set(tool.name, tool); },
				getActiveTools: () => [...tools.keys()],
				setActiveTools(_names: string[]) {},
				async sendUserMessage(_text: string) {},
				logger: { error() {} },
			};
			extension(pi);
			handlers.get("session_start")!(undefined, {
				sessionManager: { getSessionId: () => randomUUID() },
				isIdle: () => true, hasPendingMessages: () => false,
				ui: { getEditorText: () => "" }, abort: () => {},
			});
			const tool = tools.get("workbench_logs")!;
			assert.ok(tool);
			assert.equal(tool.strict, true);
			assert.equal(tool.loadMode, "essential");
			assert.deepEqual(tool.parameters.properties.action.enum, ["status", "list", "show", "raw", "raw-status"]);
			assert.deepEqual(tool.parameters.properties.offset.type, ["integer", "null"]);
			assert.deepEqual(tool.parameters.properties.length.type, ["integer", "null"]);
			assert.equal(tool.parameters.additionalProperties, false);
			assert.equal(tools.has("archival-close"), false);
			assert.equal(tools.has("delete-db"), false);
			const helloDeadline = Date.now() + 1500;
			while (!seen.some(frame => frame.kind === "hello") && Date.now() < helloDeadline) await delay(5);
			assert.ok(seen.some(frame => frame.kind === "hello"));
			const args = { action: "status", subject_kind: null, subject_id: null, limit: null, page: null,
				offset: null, length: null };
			const result = await tool.execute("dvl07-call", args, new AbortController().signal, () => {}, {});
			const answer = JSON.parse(result.content[0].text);
			assert.equal(answer.status, "ok");
			assert.equal(answer.data.databaseCount, 1);
			const deadline = Date.now() + 1500;
			while (!seen.some(frame => frame.kind === "tool_request") && Date.now() < deadline) await delay(5);
			const calls = seen.filter(frame => frame.kind === "tool_request");
			assert.equal(calls.length, 1);
			assert.equal(calls[0].tool, "workbench_logs");
			assert.deepEqual(calls[0].args, args);
		} finally {
			handlers.get("session_shutdown")?.();
			for (const socket of sockets) socket.destroy();
			await new Promise<void>(resolve => server.close(() => resolve()));
			for (const name of names) {
				const value = previous.get(name);
				if (value === undefined) delete process.env[name]; else process.env[name] = value;
			}
			await rm(directory, { recursive: true, force: true });
		}
	}
});
