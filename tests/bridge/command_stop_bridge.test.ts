// CS-06 command-stop bridge tests (seam S4): the worker `terminal` stop argument and the manager `stop_command`
// tool against a fake backend peer on a real Unix socket. No OMP, no provider. Expected values come from
// docs/features/command-stop/SPEC.md (argument names, 45 s / 10 s / 150 s result timeouts, strict schema), not
// from bridge.ts constants.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { test } from "node:test";
import workbenchG3Extension, { terminalTimeoutMs } from "../../omp_bridge/g3/bridge.ts";

type Frame = Record<string, any>;
const SESSION_ID = "30000000-0000-4000-8000-0000000cs006";

async function startBridge(role: "manager" | "worker") {
	const directory = await mkdtemp(join(tmpdir(), "cs06-bridge-"));
	const socketPath = join(directory, "bridge.sock");
	const frames: Frame[] = [];
	const sockets: net.Socket[] = [];
	const server = net.createServer(socket => {
		sockets.push(socket);
		let pending = "";
		socket.on("data", chunk => {
			pending += chunk.toString("utf8");
			while (pending.includes("\n")) {
				const index = pending.indexOf("\n");
				frames.push(JSON.parse(pending.slice(0, index)) as Frame);
				pending = pending.slice(index + 1);
			}
		});
	});
	await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(socketPath, resolve); });
	const names = ["WORKBENCH_G3_BRIDGE_SOCKET", "WORKBENCH_G3_ROLE", "WORKBENCH_G3_TOKEN", "WORKBENCH_G3_GENERATION"];
	const previous = new Map(names.map(name => [name, process.env[name]]));
	process.env.WORKBENCH_G3_BRIDGE_SOCKET = socketPath;
	process.env.WORKBENCH_G3_ROLE = role;
	process.env.WORKBENCH_G3_TOKEN = randomUUID();
	process.env.WORKBENCH_G3_GENERATION = "1";
	const handlers = new Map<string, (...args: any[]) => any>();
	const tools = new Map<string, Frame>();
	const userMessages: string[] = [];
	const omp = { idle: true, editor: "" };
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerTool(tool: Frame) { tools.set(tool.name, tool); },
		async sendUserMessage(text: string) { userMessages.push(text); },
		logger: { error() {} },
	};
	const context = {
		sessionManager: { getSessionId: () => SESSION_ID },
		isIdle: () => omp.idle, hasPendingMessages: () => false,
		ui: { getEditorText: () => omp.editor }, abort: () => {},
	};
	workbenchG3Extension(pi);
	handlers.get("session_start")!(undefined, context);
	async function waitFor(predicate: (frame: Frame) => boolean, timeoutMs = 2000): Promise<Frame> {
		const deadline = Date.now() + timeoutMs;
		while (Date.now() < deadline) {
			const index = frames.findIndex(predicate);
			if (index >= 0) return frames.splice(index, 1)[0];
			await delay(5);
		}
		throw new Error("bridge frame timed out");
	}
	await waitFor(frame => frame.kind === "hello");
	function reply(frame: Frame): void { sockets.at(-1)!.write(JSON.stringify(frame) + "\n"); }
	async function ack(requestId: string): Promise<Frame> {
		return waitFor(frame => frame.kind === "api_ack" && frame.requestId === requestId);
	}
	async function close(): Promise<void> {
		handlers.get("session_shutdown")?.();
		for (const socket of sockets) socket.destroy();
		await new Promise<void>(resolve => server.close(() => resolve()));
		for (const name of names) {
			const value = previous.get(name);
			if (value === undefined) delete process.env[name]; else process.env[name] = value;
		}
		await rm(directory, { recursive: true, force: true });
	}
	return { frames, handlers, tools, waitFor, reply, ack, close, userMessages, omp };
}

function textOf(result: Frame): Frame {
	return JSON.parse(result.content[0].text);
}

test("CS-06 (1): worker terminal schema has an optional nullable stop; a plain command is forwarded unchanged", async () => {
	const worker = await startBridge("worker");
	try {
		const terminal = worker.tools.get("terminal")!;
		const properties = terminal.parameters.properties;
		assert.ok("stop" in properties, "terminal must accept stop");
		assert.deepEqual(properties.stop.type, ["string", "null"]);
		assert.ok(!terminal.parameters.required.includes("stop"), "stop is optional");
		assert.equal(terminal.parameters.additionalProperties, false);
		assert.match(properties.stop.description, /null when not used/i);
		const pending = terminal.execute("plain-1", { command: "x" }, new AbortController().signal, () => {}, {});
		const request = await worker.waitFor(frame => frame.kind === "tool_request" && frame.tool === "terminal");
		assert.deepEqual(request.args, { command: "x" });
		worker.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "plain-1",
			result: { status: "exited", exit_code: 0 } });
		assert.equal(textOf(await pending).status, "exited");
	} finally { await worker.close(); }
});

const STOP_ID = "0b3f8a52-6d41-4c7e-9a10-5e2f7c8d9b61";

test("CS-06 (2): terminal result timeout is 45 s for a stop, 10 s for a state query, 150 s for a command", () => {
	assert.equal(terminalTimeoutMs({ command: null, stop: STOP_ID }), 45_000);
	assert.equal(terminalTimeoutMs({ command: null, stop: null }), 10_000);
	assert.equal(terminalTimeoutMs({ command: null }), 10_000);
	assert.equal(terminalTimeoutMs({ command: null, stop: "" }), 10_000, "blank stop means not used");
	assert.equal(terminalTimeoutMs({ command: "x" }), 150_000);
	assert.equal(terminalTimeoutMs({ command: "x", stop: null }), 150_000);
	assert.equal(terminalTimeoutMs({ command: null, stop: "  " }), 10_000, "whitespace-only stop means not used");
	assert.equal(terminalTimeoutMs({ command: "x", stop: " " }), 150_000, "whitespace-only stop does not shorten a command call");
});

// Records the delays of the timers armed while `run` starts a tool call (the bridge's result timer is the only
// timer of 1 s or more it arms for a call), so a test sees the limit the execute path really uses.
async function resultLimitOf(start: () => Promise<unknown>): Promise<number[]> {
	const original = globalThis.setTimeout;
	const delays: number[] = [];
	globalThis.setTimeout = ((fn: any, ms?: number, ...rest: any[]) => {
		if (typeof ms === "number" && ms >= 1000) delays.push(ms);
		return original(fn, ms, ...rest);
	}) as typeof setTimeout;
	let pending: Promise<unknown>;
	try { pending = start(); } finally { globalThis.setTimeout = original; }
	await pending.catch(() => {});
	return delays;
}

test("CS-06 (3): a stop call goes out as a terminal frame with command null and the id; exited/stopping come back as-is", async () => {
	const worker = await startBridge("worker");
	try {
		const terminal = worker.tools.get("terminal")!;
		const replies: Frame[] = [
			{ status: "exited", command_id: STOP_ID, requested_by: "worker", exit_code: 130, signal: 2, signal_name: "SIGINT",
				signals: [], residual: { checked: true, alive: [], left_session: [], unchecked: "x" } },
			{ status: "stopping", command_id: STOP_ID, requested_by: "worker", stage: "sigterm" },
		];
		for (const [index, result] of replies.entries()) {
			const callId = `stop-${index}`;
			let pending!: Promise<Frame>;
			const delays = await resultLimitOf(async () => {
				pending = terminal.execute(callId, { command: null, stop: STOP_ID }, new AbortController().signal, () => {}, {});
			});
			assert.deepEqual(delays.filter(ms => ms >= 5000), [45_000], "a stop must not be cut at 10 s or 150 s");
			const request = await worker.waitFor(frame => frame.kind === "tool_request" && frame.tool === "terminal");
			assert.equal(request.toolCallId, callId);
			assert.deepEqual(request.args, { command: null, stop: STOP_ID });
			worker.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: callId, result });
			assert.deepEqual(textOf(await pending), result);
		}
		// A plain command and a state query keep their own limits through the same execute path.
		for (const [args, expected] of [[{ command: "x" }, 150_000], [{ command: null, stop: null }, 10_000]] as const) {
			let pending!: Promise<Frame>;
			const delays = await resultLimitOf(async () => {
				pending = terminal.execute("other", args, new AbortController().signal, () => {}, {});
			});
			assert.deepEqual(delays.filter(ms => ms >= 5000), [expected]);
			const request = await worker.waitFor(frame => frame.kind === "tool_request" && frame.tool === "terminal");
			worker.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "other", result: { status: "running" } });
			await pending;
		}
	} finally { await worker.close(); }
});

// Minimal JSON-schema check for the strict shapes used here (type, required, additionalProperties, minLength,
// maxLength, pattern); there is no schema library in the repo.
function accepts(schema: Frame, value: unknown): boolean {
	if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
	const object = value as Frame;
	for (const key of schema.required ?? []) if (!(key in object)) return false;
	for (const [key, field] of Object.entries(object)) {
		const rule = schema.properties[key];
		if (rule === undefined) { if (schema.additionalProperties === false) return false; continue; }
		const types: string[] = Array.isArray(rule.type) ? rule.type : [rule.type];
		const actual = field === null ? "null" : typeof field;
		if (!types.includes(actual)) return false;
		if (typeof field === "string") {
			if (rule.minLength !== undefined && field.length < rule.minLength) return false;
			if (rule.maxLength !== undefined && field.length > rule.maxLength) return false;
			if (rule.pattern !== undefined && !new RegExp(rule.pattern).test(field)) return false;
		}
	}
	return true;
}

test("CS-06 (4): manager registers a strict stop_command; the worker lacks it and the manager lacks terminal", async () => {
	const worker = await startBridge("worker");
	try {
		assert.ok(!worker.tools.has("stop_command"), "stop_command is manager-only");
		assert.ok(worker.tools.has("terminal"));
	} finally { await worker.close(); }
	const manager = await startBridge("manager");
	try {
		assert.ok(!manager.tools.has("terminal"), "terminal is worker-only");
		const stop = manager.tools.get("stop_command")!;
		assert.ok(stop, "manager must have stop_command");
		assert.equal(stop.strict, true);
		assert.equal(stop.loadMode, "essential");
		const schema = stop.parameters;
		assert.equal(schema.type, "object");
		assert.equal(schema.additionalProperties, false);
		assert.deepEqual([...schema.required].sort(), ["command_id", "reason"]);
		assert.deepEqual(Object.keys(schema.properties).sort(), ["command_id", "reason"]);
		assert.equal(schema.properties.command_id.type, "string");
		assert.equal(schema.properties.reason.type, "string");
		assert.equal(schema.properties.reason.minLength, 1);
		assert.equal(schema.properties.reason.maxLength, 500);
		// argument validation against the exposed schema
		assert.equal(accepts(schema, { command_id: STOP_ID, reason: "stuck" }), true);
		assert.equal(accepts(schema, { command_id: STOP_ID, reason: "r".repeat(500) }), true);
		assert.equal(accepts(schema, { command_id: STOP_ID, reason: "" }), false, "reason is 1..500");
		assert.equal(accepts(schema, { command_id: STOP_ID, reason: "r".repeat(501) }), false);
		assert.equal(accepts(schema, { command_id: STOP_ID }), false, "reason is required");
		assert.equal(accepts(schema, { reason: "stuck" }), false, "command_id is required");
		assert.equal(accepts(schema, { command_id: "not-a-uuid", reason: "stuck" }), false);
		assert.equal(accepts(schema, { command_id: null, reason: "stuck" }), false);
		assert.equal(accepts(schema, { command_id: STOP_ID, reason: "stuck", extra: 1 }), false);
		assert.match(stop.description, /command_id/);
		assert.match(stop.description, /reason/);
	} finally { await manager.close(); }
});

test("CS-06 (4): stop_command waits 45 s; the other manager tools keep 10 s; the frame is forwarded as given", async () => {
	const manager = await startBridge("manager");
	try {
		const stop = manager.tools.get("stop_command")!;
		const args = { command_id: STOP_ID, reason: "runaway test" };
		let pending!: Promise<Frame>;
		const delays = await resultLimitOf(async () => {
			pending = stop.execute("stop-m1", args, new AbortController().signal, () => {}, {});
		});
		assert.deepEqual(delays.filter(ms => ms >= 5000), [45_000]);
		const request = await manager.waitFor(frame => frame.kind === "tool_request" && frame.tool === "stop_command");
		assert.deepEqual(request.args, args);
		assert.equal(request.toolCallId, "stop-m1");
		const result = { status: "exited", command_id: STOP_ID, requested_by: "manager", exit_code: 143 };
		manager.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "stop-m1", result });
		assert.deepEqual(textOf(await pending), result);
		const others: Array<[string, Frame]> = [["restart_worker", { reason: "x" }], ["workbench_status", {}],
			["stop_survivor", { survivor_id: "abc", reason: "x" }], ["to_worker", { kind: "work", message: "m" }]];
		for (const [name, params] of others) {
			const tool = manager.tools.get(name)!;
			let other!: Promise<Frame>;
			const seen = await resultLimitOf(async () => {
				other = tool.execute(`o-${name}`, params, new AbortController().signal, () => {}, {});
			});
			assert.deepEqual(seen.filter(ms => ms >= 5000), [10_000], name);
			const frame = await manager.waitFor(item => item.kind === "tool_request" && item.tool === name);
			manager.reply({ kind: "tool_result", requestId: frame.requestId, toolCallId: `o-${name}`, result: { status: "ok" } });
			await other;
		}
	} finally { await manager.close(); }
});

test("CS-06 (4): a subagent's stop_command is rejected and nothing is sent", async () => {
	const manager = await startBridge("manager");
	try {
		const subagent = { agent: { kind: "sub", depth: 1, parentId: "p", name: "explore" } };
		const result = textOf(await manager.tools.get("stop_command")!.execute("sub-1",
			{ command_id: STOP_ID, reason: "x" }, new AbortController().signal, () => {}, subagent));
		assert.equal(result.status, "rejected");
		assert.equal(result.reason, "subagent_not_allowed");
		await delay(30);
		assert.deepEqual(manager.frames.filter(frame => frame.kind === "tool_request"), []);
	} finally { await manager.close(); }
	const worker = await startBridge("worker");
	try {
		const subagent = { agent: { kind: "sub", depth: 1, parentId: "p", name: "explore" } };
		const result = textOf(await worker.tools.get("terminal")!.execute("sub-2",
			{ command: null, stop: STOP_ID }, new AbortController().signal, () => {}, subagent));
		assert.equal(result.reason, "subagent_not_allowed", "a subagent cannot stop through terminal either");
		await delay(30);
		assert.deepEqual(worker.frames.filter(frame => frame.kind === "tool_request"), []);
	} finally { await worker.close(); }
});

// Behavior 5 (documentation, TDD exception): static checks of the model-facing text. The expected phrases come
// from the SPEC contract (stop is not a new command, cancel does not stop, the result statuses and stopped_by),
// not from the wording in bridge.ts or the skill files.
const SKILLS = new URL("../../omp_bridge/skills/", import.meta.url);
async function skillText(path: string): Promise<string> { return readFile(new URL(path, SKILLS), "utf8"); }
const RESULT_STATUSES = ["exited", "unknown", "already_exited", "not_found", "stale", "refused", "stopping"];

test("CS-06 (5): tool descriptions explain stop and match the exposed schemas", async () => {
	const worker = await startBridge("worker");
	try {
		const terminal = worker.tools.get("terminal")!;
		const description = terminal.description as string;
		assert.match(description, /\bstop\b/);
		assert.match(description, /not a new command/i);
		assert.match(description, /works while (?:that |the )?command (?:runs|is running|occupies)|even while/i, "works while the command occupies the host terminal");
		assert.match(description, /waits? for the (?:stop )?result|waits? until/i);
		assert.match(description, /command_id/);
		assert.match(terminal.parameters.properties.stop.description, /command_id/);
		assert.match(terminal.parameters.properties.stop.description, /not a new command/i);
		assert.match(description, /stopping/);
	} finally { await worker.close(); }
	const manager = await startBridge("manager");
	try {
		const toWorker = manager.tools.get("to_worker")!.description as string;
		assert.match(toWorker, /cancel (?:does not|doesn't) stop/i);
		assert.match(toWorker, /stop_command/);
		const stop = manager.tools.get("stop_command")!;
		for (const name of Object.keys(stop.parameters.properties)) assert.match(stop.description, new RegExp(name), name);
		for (const status of ["exited", "unknown", "already_exited", "not_found", "stale", "refused", "stopping"]) {
			assert.match(stop.description, new RegExp(status), status);
		}
		assert.match(stop.description, /cancel/i, "says a Task cancel does not stop its command");
	} finally { await manager.close(); }
});

test("CS-06 (5): the worker's terminal reference documents stop, stopping, stopped_by and where command ids come from", async () => {
	const text = await skillText("to-manager/references/terminal.md");
	assert.match(text, /`stop`/);
	assert.match(text, /not a new command/i);
	assert.match(text, /\bstopping\b/);
	assert.match(text, /\bstopped_by\b/);
	for (const status of RESULT_STATUSES) assert.match(text, new RegExp(`\`${status}\``), status);
	for (const source of ["terminal_check", "terminal_done", "commands_run", "command_id"]) {
		assert.match(text, new RegExp(source), source);
	}
	assert.match(text, /do not (?:re-?run|run it again)/i, "a command the manager stopped is not rerun as a failure");
	assert.match(text, /with `command` null|`command` null/i, "stop is given with command null");
});

test("CS-06 (5): the manager's to-worker references say cancel does not stop a command and name stop_command and host_commands", async () => {
	const combined = [await skillText("to-worker/references/task-fields.md"),
		await skillText("to-worker/references/task-results.md")].join("\n");
	assert.match(combined, /cancel(?:ling)? (?:does not|doesn't) stop/i);
	assert.match(combined, /`stop_command`/);
	assert.match(combined, /`host_commands`/);
	assert.match(combined, /`command_id`/);
	assert.match(combined, /run_id/);
	assert.match(combined, /host_command_note/);
});

test("CS-06 (5): workbench-recovery covers stop_command, cancel is not stop and host_commands", async () => {
	const text = await skillText("workbench-recovery/SKILL.md");
	assert.match(text, /`stop_command`/);
	assert.match(text, /`host_commands`/);
	assert.match(text, /cancel(?:ling)? (?:does not|doesn't|is not|≠) ?stop|cancel is not (?:a )?stop|cancel does not stop/i);
	for (const status of ["unknown", "refused", "stale"]) assert.match(text, new RegExp(`\`${status}\``), status);
	assert.match(text, /`stop_survivor`/, "stop_survivor stays the tool for a previous backend's leftovers");
	assert.match(text, /`command_id`/);
});
