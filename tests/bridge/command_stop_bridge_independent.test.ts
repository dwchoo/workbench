// CS-06 independent bridge tests (cs06-test-01): the worker `terminal` stop argument and the manager `stop_command`
// tool against a fake backend peer on a real Unix socket (seam S4). No OMP, no provider.
// Expected values come from docs/features/command-stop/SPEC.md ("Worker: terminal tool's stop argument", "Manager:
// stop_command", result contract, timeouts) and CS-06.md, never from bridge.ts constants:
//   stop result timeout 45_000 ms; command:null without stop 10_000 ms; command execution 150_000 ms (120 s wait + 30 s
//   slack); every other Manager tool 10_000 ms.
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
const SESSION_ID = "30000000-0000-4000-8000-0000000cs0b6";
const CMD_ID = "0b3f8a52-6d41-4c7e-9a10-5e2f7c8d9b61";
const OTHER_ID = "7d1e3c9a-2b58-4f06-8e31-a4c9d27b5e10";

// SPEC-fixed limits.
const STOP_MS = 45_000;
const QUERY_MS = 10_000;
const COMMAND_MS = 150_000;
const OTHER_MANAGER_TOOL_MS = 10_000;

const SPEC_STATUSES = ["exited", "unknown", "already_exited", "not_found", "stale", "refused", "stopping"];

interface BridgeOptions { subagentSession?: boolean }

async function startBridge(role: "manager" | "worker", options: BridgeOptions = {}) {
	const directory = await mkdtemp(join(tmpdir(), "cs06i-bridge-"));
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
	const activeToolCalls: string[][] = [];
	const omp = { idle: true, editor: "" };
	const pi = {
		on(name: string, handler: (...args: any[]) => any) { handlers.set(name, handler); },
		registerTool(tool: Frame) { tools.set(tool.name, tool); },
		async sendUserMessage() {},
		getActiveTools: () => ["read", "terminal", "stop_command", "to_worker", "to_manager", "stop_survivor"],
		setActiveTools(list: string[]) { activeToolCalls.push(list); },
		logger: { error() {} },
	};
	const context: Frame = {
		sessionManager: { getSessionId: () => SESSION_ID },
		isIdle: () => omp.idle, hasPendingMessages: () => false,
		ui: { getEditorText: () => omp.editor }, abort: () => {},
	};
	if (options.subagentSession) context.agent = { kind: "sub", depth: 1, parentId: "p" };
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
	const hello = options.subagentSession ? undefined : await waitFor(frame => frame.kind === "hello");
	function reply(frame: Frame): void { sockets.at(-1)!.write(JSON.stringify(frame) + "\n"); }
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
	return { frames, handlers, tools, waitFor, reply, close, hello, activeToolCalls };
}

function textOf(result: Frame): Frame { return JSON.parse(result.content[0].text); }

// Arms are recorded only for the synchronous start of a call: the bridge arms exactly one result timer per tool call
// there. The recorded callback lets a test fire "the timeout" without waiting 45 s; the real timer is unref'd.
function armedTimers(start: () => Promise<Frame>): { armed: { ms: number; fn: () => void }[]; pending: Promise<Frame> } {
	const original = globalThis.setTimeout;
	const armed: { ms: number; fn: () => void }[] = [];
	globalThis.setTimeout = ((fn: any, ms?: number, ...rest: any[]) => {
		const handle = original(fn, ms, ...rest);
		if (typeof ms === "number" && ms >= 1000) { armed.push({ ms, fn }); handle.unref?.(); }
		return handle;
	}) as typeof setTimeout;
	let pending!: Promise<Frame>;
	try { pending = start(); } finally { globalThis.setTimeout = original; }
	return { armed, pending };
}

// Minimal JSON-schema checker for the keywords the bridge schemas use (type, required, additionalProperties,
// properties, pattern, minLength, maxLength). It stands in for OMP's strict-schema validation.
function validate(schema: Frame, value: unknown, path = "$"): string[] {
	const errors: string[] = [];
	const types: string[] = Array.isArray(schema.type) ? schema.type : [schema.type];
	const actual = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
	if (!types.includes(actual)) return [`${path}: type ${actual} not in ${types}`];
	if (actual === "string") {
		const text = value as string;
		if (schema.pattern !== undefined && !new RegExp(schema.pattern).test(text)) errors.push(`${path}: pattern`);
		if (schema.minLength !== undefined && text.length < schema.minLength) errors.push(`${path}: minLength`);
		if (schema.maxLength !== undefined && text.length > schema.maxLength) errors.push(`${path}: maxLength`);
	}
	if (actual === "object") {
		const object = value as Frame;
		for (const key of schema.required ?? []) if (!(key in object)) errors.push(`${path}.${key}: required`);
		for (const key of Object.keys(object)) {
			if (!(key in (schema.properties ?? {}))) {
				if (schema.additionalProperties === false) errors.push(`${path}.${key}: additional`);
			} else errors.push(...validate(schema.properties[key], object[key], `${path}.${key}`));
		}
	}
	return errors;
}

const SUBAGENT_CONTEXTS: [string, Frame][] = [
	["kind sub", { agent: { kind: "sub" } }],
	["depth > 0", { agent: { depth: 2 } }],
	["parentId", { agent: { parentId: "root-agent" } }],
	["unreadable identity", { get agent(): never { throw new Error("unreadable"); } }],
];

// ---------------------------------------------------------------------------------------------------------------
// Worker terminal schema

test("CS-06 S4 schema: worker terminal gains an optional nullable uuid stop; strict; command stays required", async () => {
	const worker = await startBridge("worker");
	try {
		const terminal = worker.tools.get("terminal")!;
		assert.deepEqual(Object.keys(terminal.parameters.properties).sort(), ["command", "stop"]);
		assert.deepEqual(terminal.parameters.required, ["command"], "stop must not be required");
		assert.equal(terminal.parameters.additionalProperties, false);
		assert.equal(terminal.strict, true);
		assert.equal(terminal.loadMode, "essential");
		const stop = terminal.parameters.properties.stop;
		assert.deepEqual([...stop.type].sort(), ["null", "string"], "optional fields are nullable in a strict schema");
		assert.match(stop.description, /null when not used/i);
		// `stop` carries a command_id: uuid or blank (blank = not used, SPEC) or null; nothing else.
		for (const accepted of [CMD_ID, "", null]) assert.deepEqual(validate(terminal.parameters, { command: null, stop: accepted }), [], String(accepted));
		for (const rejected of ["not-a-uuid", CMD_ID + "x", " " + CMD_ID, CMD_ID + "\n", 7, true, {}, []]) {
			assert.notDeepEqual(validate(terminal.parameters, { command: null, stop: rejected }), [], JSON.stringify(rejected));
		}
		assert.notDeepEqual(validate(terminal.parameters, { command: null, stop: CMD_ID, extra: 1 }), [], "strict: no extra key");
		// the old call shapes still validate
		assert.deepEqual(validate(terminal.parameters, { command: "x" }), []);
		assert.deepEqual(validate(terminal.parameters, { command: null }), []);
		assert.notDeepEqual(validate(terminal.parameters, { stop: CMD_ID }), [], "command remains required (null for a stop)");
	} finally { await worker.close(); }
});

test("CS-06 S4 schema: terminal description explains stop (not a new command, works while running, waits, statuses)", async () => {
	const worker = await startBridge("worker");
	try {
		const terminal = worker.tools.get("terminal")!;
		const description = terminal.description as string;
		assert.match(description, /command_id/);
		assert.match(description, /stop/i);
		assert.match(description, /not a new command/i);
		assert.match(description, /occupies the host terminal|while (that|the) command (runs|is running|occupies)/i);
		assert.match(description, /waits? for the stop result/i);
		for (const status of SPEC_STATUSES) assert.ok(description.includes(status), `description names ${status}`);
		assert.match(description, /command null/i, "a stop is given with command null");
		// the old command guidance is preserved
		assert.match(description, /120 s/);
		assert.match(description, /end your turn/i);
		assert.doesNotMatch(JSON.stringify(terminal), /[가-힣]/, "model-facing text is English");
		assert.doesNotMatch(JSON.stringify(terminal), /timeout_seconds|1800/);
	} finally { await worker.close(); }
});

// ---------------------------------------------------------------------------------------------------------------
// Timeouts

test("CS-06 S4 timeouts: terminalTimeoutMs for every argument combination (SPEC values)", () => {
	// stop = non-empty string
	assert.equal(terminalTimeoutMs({ command: null, stop: CMD_ID }), STOP_MS);
	assert.equal(terminalTimeoutMs({ stop: CMD_ID }), STOP_MS, "command key absent");
	assert.equal(terminalTimeoutMs({ command: null, stop: CMD_ID, i: "intent" }), STOP_MS);
	// stop = null / blank / absent -> as before
	assert.equal(terminalTimeoutMs({ command: null, stop: null }), QUERY_MS);
	assert.equal(terminalTimeoutMs({ command: null, stop: "" }), QUERY_MS, "blank stop = not used");
	assert.equal(terminalTimeoutMs({ command: null }), QUERY_MS);
	assert.equal(terminalTimeoutMs({ command: "x" }), COMMAND_MS);
	assert.equal(terminalTimeoutMs({ command: "x", stop: null }), COMMAND_MS);
	assert.equal(terminalTimeoutMs({ command: "x", stop: "" }), COMMAND_MS, "blank stop does not shorten a command call");
	// a non-string stop is not a stop
	for (const stop of [0, 1, true, false, {}, [], [CMD_ID]]) {
		assert.equal(terminalTimeoutMs({ command: null, stop }), QUERY_MS, JSON.stringify(stop));
		assert.equal(terminalTimeoutMs({ command: "x", stop }), COMMAND_MS, JSON.stringify(stop));
	}
	// malformed params keep the command-call limit (legacy behaviour)
	for (const params of [undefined, null, "x", 7, []]) assert.equal(terminalTimeoutMs(params), COMMAND_MS, String(params));
	// ticket CS-06 (1): stop non-empty string -> 45 s, whatever command is. Backend answers invalid_arguments at once.
	assert.equal(terminalTimeoutMs({ command: "x", stop: CMD_ID }), STOP_MS);
	// the stop wait is longer than the old query/command-less limits, and fits inside the 120 s wait
	assert.ok(STOP_MS > QUERY_MS && STOP_MS < 120_000);
});

test("CS-06 S4 timeouts: the execute path arms exactly the SPEC limit for each worker terminal call", async () => {
	const worker = await startBridge("worker");
	try {
		const terminal = worker.tools.get("terminal")!;
		const cases: [string, Frame, number][] = [
			["stop", { command: null, stop: CMD_ID }, STOP_MS],
			["stop with intent field", { command: null, stop: CMD_ID, i: "why" }, STOP_MS],
			["query", { command: null }, QUERY_MS],
			["query, stop null", { command: null, stop: null }, QUERY_MS],
			["query, stop blank", { command: null, stop: "" }, QUERY_MS],
			["command", { command: "make" }, COMMAND_MS],
			["command, stop null", { command: "make", stop: null }, COMMAND_MS],
		];
		for (const [label, args, expected] of cases) {
			const callId = `t-${randomUUID()}`;
			const { armed, pending } = armedTimers(() => terminal.execute(callId, args, new AbortController().signal, () => {}, {}));
			assert.deepEqual(armed.map(timer => timer.ms), [expected], `${label}: only the SPEC limit, not 10 s/120 s`);
			const request = await worker.waitFor(frame => frame.kind === "tool_request" && frame.toolCallId === callId);
			worker.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: callId, result: { status: "exited" } });
			assert.equal(textOf(await pending).status, "exited", label);
		}
	} finally { await worker.close(); }
});

test("CS-06 S4 timeouts: a stop that times out returns outcome_unknown and tells the backend the wait is abandoned", async () => {
	const worker = await startBridge("worker");
	try {
		const callId = "stop-timeout";
		const { armed, pending } = armedTimers(() => worker.tools.get("terminal")!
			.execute(callId, { command: null, stop: CMD_ID }, new AbortController().signal, () => {}, {}));
		const request = await worker.waitFor(frame => frame.kind === "tool_request" && frame.tool === "terminal");
		assert.equal(armed.length, 1);
		armed[0].fn();  // the 45 s result limit elapses
		const result = textOf(await pending);
		assert.equal(result.status, "outcome_unknown");
		assert.equal(result.reason, "timeout");
		const abandoned = await worker.waitFor(frame => frame.kind === "tool_request" && frame.tool === "terminal_wait_abandoned");
		assert.deepEqual(abandoned.args, { tool_call_id: callId });
		assert.equal(abandoned.sessionId, SESSION_ID);
		// a late backend answer must not break the bridge: the next call still works
		worker.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: callId, result: { status: "exited" } });
		const next = worker.tools.get("terminal")!.execute("after", { command: null }, new AbortController().signal, () => {}, {});
		const nextRequest = await worker.waitFor(frame => frame.kind === "tool_request" && frame.toolCallId === "after");
		worker.reply({ kind: "tool_result", requestId: nextRequest.requestId, toolCallId: "after", result: { status: "running" } });
		assert.equal(textOf(await next).status, "running");
	} finally { await worker.close(); }
});

// ---------------------------------------------------------------------------------------------------------------
// Worker frames and results

test("CS-06 S4 frame: stop goes out as tool terminal with args exactly {command:null, stop:id}; plain command unchanged", async () => {
	const worker = await startBridge("worker");
	try {
		const terminal = worker.tools.get("terminal")!;
		const plain = terminal.execute("plain", { command: "x" }, new AbortController().signal, () => {}, {});
		const plainFrame = await worker.waitFor(frame => frame.kind === "tool_request" && frame.toolCallId === "plain");
		assert.deepEqual(plainFrame.args, { command: "x" }, "no stop key appears on a plain command");
		worker.reply({ kind: "tool_result", requestId: plainFrame.requestId, toolCallId: "plain", result: { status: "exited" } });
		await plain;

		const stopCall = terminal.execute("stop-1", { command: null, stop: CMD_ID }, new AbortController().signal, () => {}, {});
		const stopFrame = await worker.waitFor(frame => frame.kind === "tool_request" && frame.toolCallId === "stop-1");
		assert.equal(stopFrame.kind, "tool_request");
		assert.equal(stopFrame.tool, "terminal");
		assert.deepEqual(stopFrame.args, { command: null, stop: CMD_ID });
		assert.equal(stopFrame.toolCallId, "stop-1");
		assert.equal(stopFrame.sessionId, SESSION_ID);
		assert.equal(stopFrame.generation, worker.hello!.generation);
		assert.deepEqual(Object.keys(stopFrame).sort(), Object.keys(plainFrame).sort(), "same envelope as any terminal call");
		assert.match(stopFrame.requestId, /^[0-9a-f-]{36}$/);
		assert.notEqual(stopFrame.requestId, plainFrame.requestId);
		worker.reply({ kind: "tool_result", requestId: stopFrame.requestId, toolCallId: "stop-1", result: { status: "exited" } });
		await stopCall;

		// OMP's intent field `i` is not a Workbench argument; a null stop is forwarded as given (backend: not used)
		const withIntent = terminal.execute("stop-2", { command: null, stop: CMD_ID, i: "stopping it" }, new AbortController().signal, () => {}, {});
		const intentFrame = await worker.waitFor(frame => frame.toolCallId === "stop-2");
		assert.deepEqual(intentFrame.args, { command: null, stop: CMD_ID });
		worker.reply({ kind: "tool_result", requestId: intentFrame.requestId, toolCallId: "stop-2", result: { status: "exited" } });
		await withIntent;
		const nullStop = terminal.execute("stop-3", { command: null, stop: null }, new AbortController().signal, () => {}, {});
		const nullFrame = await worker.waitFor(frame => frame.toolCallId === "stop-3");
		assert.deepEqual(nullFrame.args, { command: null, stop: null });
		worker.reply({ kind: "tool_result", requestId: nullFrame.requestId, toolCallId: "stop-3", result: { status: "running" } });
		await nullStop;
	} finally { await worker.close(); }
});

test("CS-06 S4 frame: command and stop together are forwarded untouched; the backend's invalid_arguments comes back as-is", async () => {
	const worker = await startBridge("worker");
	try {
		const call = worker.tools.get("terminal")!.execute("both", { command: "x", stop: CMD_ID }, new AbortController().signal, () => {}, {});
		const request = await worker.waitFor(frame => frame.toolCallId === "both");
		assert.deepEqual(request.args, { command: "x", stop: CMD_ID }, "the bridge does not drop or reinterpret either field");
		const refusal = { status: "rejected", reason: "invalid_arguments", fields: ["command", "stop"] };
		worker.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "both", result: refusal });
		assert.deepEqual(textOf(await call), refusal);
	} finally { await worker.close(); }
});

const BACKEND_REPLIES: Frame[] = [
	{ status: "exited", command_id: CMD_ID, requested_by: "worker", exit_code: 130, signal: 2, signal_name: "SIGINT",
		signals: [{ signal: "SIGINT", at: "2026-10-21T00:00:00Z", target: "pgid" }],
		residual: { checked: true, alive: [], left_session: [], unchecked: "processes started outside the host shell session are not checked" } },
	{ status: "exited", command_id: CMD_ID, requested_by: "worker", exit_code: 143, signal: 15, signal_name: "SIGTERM", signals: [],
		residual: { checked: true, alive: [{ pid: 7, start_ticks: 9 }], left_session: [], unchecked: "x" }, repeat: true },
	{ status: "exited", command_id: CMD_ID, requested_by: "manager", exit_code: 137, signal: 9, signal_name: "SIGKILL", stopped_by: "manager",
		corrected: true, history: [{ status: "unknown", reason: "still_alive_after_sigkill" }], signals: [], residual: { checked: true } },
	{ status: "stopping", command_id: CMD_ID, requested_by: "worker", stage: "sigterm", signals: [] },
	{ status: "unknown", command_id: CMD_ID, requested_by: "worker", reason: "still_alive_after_sigkill", signals: [], residual: { checked: true } },
	{ status: "unknown", command_id: CMD_ID, requested_by: "worker", reason: "permission_denied", signals: [], residual: { checked: true } },
	{ status: "already_exited", command_id: CMD_ID, requested_by: "worker", exit_code: 0, signal: null, ended_at: "2026-10-21T00:00:00Z" },
	{ status: "not_found", command_id: CMD_ID, requested_by: "worker", reason: "unknown_command" },
	{ status: "stale", command_id: CMD_ID, requested_by: "worker", reason: "identity_mismatch" },
	{ status: "refused", command_id: CMD_ID, requested_by: "worker", reason: "not_owner" },
	{ status: "refused", command_id: CMD_ID, requested_by: "worker", reason: "previous_backend_command" },
	{ status: "refused", command_id: CMD_ID, requested_by: "worker", reason: "backend_shutdown" },
];

test("CS-06 S4 results: every backend stop status reaches the worker unchanged (content text and details)", async () => {
	const worker = await startBridge("worker");
	try {
		for (const [index, backendResult] of BACKEND_REPLIES.entries()) {
			const callId = `r-${index}`;
			const call = worker.tools.get("terminal")!.execute(callId, { command: null, stop: CMD_ID }, new AbortController().signal, () => {}, {});
			const request = await worker.waitFor(frame => frame.toolCallId === callId);
			worker.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: callId, result: backendResult });
			const result = await call;
			assert.deepEqual(textOf(result), backendResult, `${backendResult.status}/${backendResult.reason ?? ""}`);
			assert.deepEqual(result.details, backendResult);
		}
	} finally { await worker.close(); }
});

test("CS-06 S4 occupancy: a stop is sent and answered while a long command call is still waiting (bridge does not serialize)", async () => {
	const worker = await startBridge("worker");
	try {
		const terminal = worker.tools.get("terminal")!;
		let commandSettled = false;
		const command = terminal.execute("long", { command: "sleep 100" }, new AbortController().signal, () => {}, {})
			.then((result: Frame) => { commandSettled = true; return result; });
		const commandFrame = await worker.waitFor(frame => frame.toolCallId === "long");
		const stop = terminal.execute("stop-while", { command: null, stop: CMD_ID }, new AbortController().signal, () => {}, {});
		const stopFrame = await worker.waitFor(frame => frame.toolCallId === "stop-while");
		assert.deepEqual(stopFrame.args, { command: null, stop: CMD_ID });
		worker.reply({ kind: "tool_result", requestId: stopFrame.requestId, toolCallId: "stop-while",
			result: { status: "exited", exit_code: 130, signal: 2 } });
		assert.equal(textOf(await stop).exit_code, 130);
		assert.equal(commandSettled, false, "the stop reply must not settle the command call");
		worker.reply({ kind: "tool_result", requestId: commandFrame.requestId, toolCallId: "long",
			result: { status: "exited", exit_code: 130, stopped_by: "worker", signal: 2 } });
		assert.deepEqual(textOf(await command), { status: "exited", exit_code: 130, stopped_by: "worker", signal: 2 });
	} finally { await worker.close(); }
});

test("CS-06 S4 abandon: aborting a pending stop wait keeps the terminal_wait_abandoned signal; after an answer, nothing", async () => {
	const worker = await startBridge("worker");
	try {
		const terminal = worker.tools.get("terminal")!;
		const controller = new AbortController();
		const pending = terminal.execute("stop-abort", { command: null, stop: CMD_ID }, controller.signal, () => {}, {});
		await worker.waitFor(frame => frame.toolCallId === "stop-abort");
		controller.abort();
		const result = textOf(await pending);
		assert.equal(result.status, "outcome_unknown");
		assert.equal(result.reason, "aborted");
		const abandoned = await worker.waitFor(frame => frame.kind === "tool_request" && frame.tool === "terminal_wait_abandoned");
		assert.deepEqual(abandoned.args, { tool_call_id: "stop-abort" });
		assert.equal(abandoned.sessionId, SESSION_ID);

		const answered = new AbortController();
		const call = terminal.execute("stop-done", { command: null, stop: CMD_ID }, answered.signal, () => {}, {});
		const request = await worker.waitFor(frame => frame.toolCallId === "stop-done");
		worker.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: "stop-done", result: { status: "exited" } });
		await call;
		answered.abort();
		await delay(50);
		assert.deepEqual(worker.frames.filter(frame => frame.kind === "tool_request"), [], "abort after the answer sends nothing");
	} finally { await worker.close(); }
});

test("CS-06 S4 roles: worker has terminal but no stop_command and no manager tools", async () => {
	const worker = await startBridge("worker");
	try {
		assert.ok(worker.tools.has("terminal"));
		assert.ok(!worker.tools.has("stop_command"), "Worker must not hold stop_command (tool_not_allowed_for_role)");
		for (const name of ["stop_survivor", "restart_worker", "workbench_status", "to_worker"]) assert.ok(!worker.tools.has(name), name);
	} finally { await worker.close(); }
});

// ---------------------------------------------------------------------------------------------------------------
// Manager stop_command

test("CS-06 S4 manager: stop_command registered strict (both required, uuid, reason 1..500); Manager has no terminal", async () => {
	const manager = await startBridge("manager");
	try {
		const names = [...manager.tools.keys()];
		assert.equal(names.filter(name => name === "stop_command").length, 1);
		assert.ok(!manager.tools.has("terminal"), "Manager must not hold terminal");
		for (const legacy of ["to_worker", "restart_worker", "workbench_status", "stop_survivor", "workbench_logs"]) {
			assert.ok(manager.tools.has(legacy), `${legacy} still registered`);
		}
		const tool = manager.tools.get("stop_command")!;
		assert.equal(tool.strict, true);
		assert.equal(tool.loadMode, "essential");
		assert.equal(tool.parameters.type, "object");
		assert.equal(tool.parameters.additionalProperties, false);
		assert.deepEqual([...tool.parameters.required].sort(), ["command_id", "reason"], "both required");
		assert.deepEqual(Object.keys(tool.parameters.properties).sort(), ["command_id", "reason"]);
		assert.equal(tool.parameters.properties.command_id.type, "string", "command_id is not nullable");
		assert.equal(tool.parameters.properties.reason.type, "string", "reason is not nullable");
		assert.equal(tool.parameters.properties.reason.minLength, 1);
		assert.equal(tool.parameters.properties.reason.maxLength, 500);
		assert.match(tool.parameters.properties.command_id.description, /command_id/);
		assert.match(tool.parameters.properties.command_id.description, /run_id/, "an experiment's command_id is its run_id");
		assert.match(tool.description, /reason/i);
		assert.match(tool.description, /command_id/);
		assert.doesNotMatch(JSON.stringify(tool), /[가-힣]/, "model-facing text is English");
		for (const status of SPEC_STATUSES) assert.ok(tool.description.includes(status), `description names ${status}`);
		assert.match(tool.description, /waits? for the result/i, "the final result is awaited");

		const schema = tool.parameters;
		const ok = { command_id: CMD_ID, reason: "no longer needed" };
		assert.deepEqual(validate(schema, ok), []);
		assert.deepEqual(validate(schema, { command_id: CMD_ID, reason: "x".repeat(500) }), [], "500 accepted");
		assert.notDeepEqual(validate(schema, { command_id: CMD_ID, reason: "x".repeat(501) }), [], "501 rejected");
		assert.notDeepEqual(validate(schema, { command_id: CMD_ID, reason: "" }), [], "empty reason rejected");
		assert.notDeepEqual(validate(schema, { command_id: CMD_ID, reason: null }), [], "null reason rejected");
		assert.notDeepEqual(validate(schema, { command_id: CMD_ID }), [], "reason required");
		assert.notDeepEqual(validate(schema, { reason: "r" }), [], "command_id required");
		assert.notDeepEqual(validate(schema, { command_id: null, reason: "r" }), [], "null command_id rejected");
		assert.notDeepEqual(validate(schema, { command_id: "", reason: "r" }), [], "blank command_id rejected");
		assert.notDeepEqual(validate(schema, { command_id: "not-a-uuid", reason: "r" }), [], "non-uuid rejected");
		assert.notDeepEqual(validate(schema, { command_id: CMD_ID + "0", reason: "r" }), [], "uuid with trailing junk rejected");
		assert.notDeepEqual(validate(schema, { ...ok, extra: 1 }), [], "strict: no extra key");
		assert.notDeepEqual(validate(schema, { ...ok, stop: CMD_ID }), [], "strict: no terminal-style stop key");
	} finally { await manager.close(); }
});

test("CS-06 S4 manager: stop_command frame shape and results unchanged; timeout is 45 s", async () => {
	const manager = await startBridge("manager");
	try {
		const tool = manager.tools.get("stop_command")!;
		const args = { command_id: CMD_ID, reason: "hung" };
		for (const [index, backendResult] of BACKEND_REPLIES.entries()) {
			const callId = `m-${index}`;
			const { armed, pending } = armedTimers(() => tool.execute(callId, { ...args, i: "intent" }, new AbortController().signal, () => {}, {}));
			assert.deepEqual(armed.map(timer => timer.ms), [STOP_MS]);
			const request = await manager.waitFor(frame => frame.toolCallId === callId);
			assert.equal(request.kind, "tool_request");
			assert.equal(request.tool, "stop_command");
			assert.deepEqual(request.args, args, "exactly command_id and reason (intent field removed)");
			assert.equal(request.sessionId, SESSION_ID);
			assert.equal(request.generation, manager.hello!.generation);
			manager.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: callId,
				result: { ...backendResult, requested_by: "manager" } });
			const result = await pending;
			assert.deepEqual(textOf(result), { ...backendResult, requested_by: "manager" });
			assert.deepEqual(result.details, { ...backendResult, requested_by: "manager" });
		}
	} finally { await manager.close(); }
});

test("CS-06 S4 manager: a stop_command that times out resolves outcome_unknown/timeout at the 45 s limit, not earlier", async () => {
	const manager = await startBridge("manager");
	try {
		const { armed, pending } = armedTimers(() => manager.tools.get("stop_command")!
			.execute("m-timeout", { command_id: CMD_ID, reason: "r" }, new AbortController().signal, () => {}, {}));
		await manager.waitFor(frame => frame.toolCallId === "m-timeout");
		assert.deepEqual(armed.map(timer => timer.ms), [STOP_MS], "no 10 s timer cuts the stop wait");
		armed[0].fn();
		const result = textOf(await pending);
		assert.equal(result.status, "outcome_unknown");
		assert.equal(result.reason, "timeout");
	} finally { await manager.close(); }
});

test("CS-06 S4 manager: every other Manager tool keeps the 10 s result limit", async () => {
	const manager = await startBridge("manager");
	try {
		// workbench_records answers from a separate recent-records path (not a handoff-style call) and is out of scope.
		const others = [...manager.tools.keys()].filter(name => name !== "stop_command" && name !== "workbench_records");
		assert.ok(others.includes("to_worker") && others.includes("workbench_status") && others.includes("restart_worker")
			&& others.includes("stop_survivor") && others.includes("workbench_logs"));
		for (const name of others) {
			const callId = `o-${name}`;
			const { armed, pending } = armedTimers(() => manager.tools.get(name)!
				.execute(callId, {}, new AbortController().signal, () => {}, {}));
			assert.deepEqual(armed.map(timer => timer.ms), [OTHER_MANAGER_TOOL_MS], name);
			const request = await manager.waitFor(frame => frame.toolCallId === callId);
			manager.reply({ kind: "tool_result", requestId: request.requestId, toolCallId: callId, result: { status: "ok" } });
			await pending;
		}
	} finally { await manager.close(); }
});

test("CS-06 S4 manager: aborting a pending stop_command resolves outcome_unknown/aborted", async () => {
	const manager = await startBridge("manager");
	try {
		const controller = new AbortController();
		const pending = manager.tools.get("stop_command")!.execute("m-abort", { command_id: CMD_ID, reason: "r" }, controller.signal, () => {}, {});
		await manager.waitFor(frame => frame.toolCallId === "m-abort");
		controller.abort();
		const result = textOf(await pending);
		assert.equal(result.status, "outcome_unknown");
		assert.equal(result.reason, "aborted");
	} finally { await manager.close(); }
});

// ---------------------------------------------------------------------------------------------------------------
// Subagents

test("CS-06 S4 subagent: stop_command and terminal stop are rejected with no frame sent (every subagent identity form)", async () => {
	const manager = await startBridge("manager");
	try {
		for (const [label, context] of SUBAGENT_CONTEXTS) {
			const result = textOf(await manager.tools.get("stop_command")!.execute(`sub-m-${label}`,
				{ command_id: CMD_ID, reason: "r" }, new AbortController().signal, () => {}, context));
			assert.equal(result.status, "rejected", label);
			assert.equal(result.reason, "subagent_not_allowed", label);
		}
		await delay(50);
		assert.deepEqual(manager.frames.filter(frame => frame.kind === "tool_request"), [], "no frame for a subagent call");
	} finally { await manager.close(); }
	const worker = await startBridge("worker");
	try {
		for (const [label, context] of SUBAGENT_CONTEXTS) {
			const result = textOf(await worker.tools.get("terminal")!.execute(`sub-w-${label}`,
				{ command: null, stop: CMD_ID }, new AbortController().signal, () => {}, context));
			assert.equal(result.reason, "subagent_not_allowed", label);
		}
		await delay(50);
		assert.deepEqual(worker.frames.filter(frame => frame.kind === "tool_request"), []);
	} finally { await worker.close(); }
});

test("CS-06 S4 subagent session: stop_command is hidden from a subagent's active tools and no hello is sent", async () => {
	const manager = await startBridge("manager", { subagentSession: true });
	try {
		await delay(100);
		assert.ok(manager.activeToolCalls.length >= 1, "setActiveTools called for the subagent session");
		const kept = manager.activeToolCalls.at(-1)!;
		assert.ok(!kept.includes("stop_command"), "stop_command removed from the subagent's provider request");
		assert.ok(!kept.includes("terminal"));
		assert.ok(kept.includes("read"), "non-bridge tools stay");
		assert.deepEqual(manager.frames.filter(frame => frame.kind === "hello"), [], "a subagent session never says hello");
	} finally { await manager.close(); }
});

// ---------------------------------------------------------------------------------------------------------------
// to_worker description

test("CS-06 S4 to_worker: description says cancel does not stop a command and points to stop_command", async () => {
	const manager = await startBridge("manager");
	try {
		const description = manager.tools.get("to_worker")!.description as string;
		assert.match(description, /cancel[^.]*does not stop/i);
		assert.match(description, /stop_command/);
		assert.match(description, /host_commands/);
		// earlier guidance preserved
		assert.match(description, /do not do it yourself/i);
		assert.match(description, /wait for the worker's to_manager report/i);
	} finally { await manager.close(); }
});

// ---------------------------------------------------------------------------------------------------------------
// Documents (static)

const SKILLS = new URL("../../omp_bridge/skills/", import.meta.url);
const read = (path: string) => readFile(new URL(path, SKILLS), "utf8");

test("CS-06 docs: to-manager/references/terminal.md documents stop, its statuses, stopped_by and where ids come from", async () => {
	const doc = await read("to-manager/references/terminal.md");
	assert.match(doc, /`stop`/);
	assert.match(doc, /not a new command/i);
	assert.match(doc, /`command` null/, "a stop is given with command null");
	assert.match(doc, /`stopping`/);
	assert.match(doc, /`stopped_by`/);
	assert.match(doc, /`command_id`/);
	for (const source of ["terminal_check", "terminal_done", "commands_run"]) assert.ok(doc.includes(source), `id source ${source}`);
	for (const status of SPEC_STATUSES) assert.ok(doc.includes(`\`${status}\``), `status ${status}`);
	for (const reason of ["not_owner", "previous_backend_command", "backend_shutdown"]) assert.ok(doc.includes(reason), reason);
	assert.match(doc, /25 s/, "wait bound matches SPEC CALL_WAIT");
	assert.match(doc, /never both|not both/i, "command and stop together is invalid");
	assert.match(doc, /invalid_arguments/);
	assert.match(doc, /blank `stop`|empty `stop`/i, "blank stop = not used");
	assert.match(doc, /`stop_command`/, "the worker learns the manager can stop its command");
	// every line that names stop_command speaks of the manager (the worker never calls it)
	for (const line of doc.split("\n").filter(text => text.includes("stop_command"))) assert.match(line, /manager/i);
	assert.doesNotMatch(doc, /[가-힣]/);
});

test("CS-06 docs: to-worker task-fields.md says cancel does not stop a command; stop_command and host_commands named", async () => {
	const doc = await read("to-worker/references/task-fields.md");
	const cancelLine = doc.split("\n").find(line => line.includes("`cancel: true`"))!;
	assert.ok(cancelLine, "cancel field line exists");
	assert.match(cancelLine, /cancel does not stop/i);
	assert.match(cancelLine, /`stop_command`/);
	assert.match(cancelLine, /`command_id`/);
	assert.match(cancelLine, /`reason`/);
	assert.match(cancelLine, /`run_id`/);
	assert.match(cancelLine, /`host_commands`/);
	assert.doesNotMatch(doc, /[가-힣]/);
});

test("CS-06 docs: to-worker task-results.md covers cancel responses, stop_command results, and keeps the Results list intact", async () => {
	const doc = await read("to-worker/references/task-results.md");
	assert.match(doc, /`host_commands`/);
	assert.match(doc, /`host_command_note`/);
	assert.match(doc, /`stop_command`/);
	assert.match(doc, /does not stop/i);
	assert.match(doc, /cancel_waiting_for_host_exit/);
	for (const status of SPEC_STATUSES) assert.ok(doc.includes(`\`${status}\``), `status ${status}`);
	assert.match(doc, /`repeat: true`/);
	assert.match(doc, /25 s/);
	assert.doesNotMatch(doc, /[가-힣]/);
	// the response must never be described as saying the command stopped/terminated
	assert.doesNotMatch(doc, /cancel(led|ling)?[^.\n]*(stops|terminates|kills) (the|its) command/i);
	// anchors used inside the doc resolve to a heading
	const slug = (heading: string) => heading.toLowerCase().replace(/[^a-z0-9_ -]/g, "").trim().replace(/ /g, "-");
	const headings = doc.split("\n").filter(line => /^#{1,6} /.test(line)).map(line => slug(line.replace(/^#+ /, "")));
	for (const anchor of [...doc.matchAll(/\]\(#([^)]+)\)/g)].map(match => match[1])) assert.ok(headings.includes(anchor), `anchor #${anchor}`);
	// Structure: the pre-existing Results bullets (worker_busy, rejected, held, After a done/blocked report) must stay
	// in the "## Results" section; inserting a new section in the middle of the list must not orphan them.
	const sections = new Map<string, string>();
	let current = "";
	for (const line of doc.split("\n")) {
		const heading = /^## (.*)$/.exec(line);
		if (heading) { current = heading[1]; sections.set(current, ""); } else if (current) sections.set(current, sections.get(current) + line + "\n");
	}
	const results = sections.get("Results")!;
	for (const marker of ["`worker_busy`", "`rejected`", "`held` (nothing was sent", "After a `done`/`blocked` report"]) {
		assert.ok(results.includes(marker), `${marker} belongs to "## Results", not to a later section`);
	}
	const stopSection = [...sections.entries()].find(([name]) => name.includes("stop_command"))![1];
	assert.ok(!stopSection.includes("`worker_busy`"), "the stop_command section holds only stop results");
});

test("CS-06 docs: workbench-recovery states cancel is not stop, names stop_command, host_commands and stop_survivor", async () => {
	const doc = await read("workbench-recovery/SKILL.md");
	const front = /^---\n([\s\S]*?)\n---\n/.exec(doc)!;
	assert.ok(front, "front matter intact");
	assert.match(front[1], /^name: workbench-recovery$/m);
	assert.match(front[1], /^description: .*stop_command/m);
	assert.match(doc, /Cancel is not stop|Cancelling[^.]*does not stop/i);
	assert.match(doc, /`stop_command`/);
	assert.match(doc, /`host_commands`/);
	assert.match(doc, /`stop_survivor`/);
	assert.match(doc, /`command_id`/);
	assert.match(doc, /`reason`/);
	assert.match(doc, /`run_id`/);
	assert.match(doc, /`workbench_status`/);
	assert.match(doc, /`unknown`/);
	assert.doesNotMatch(doc, /[가-힣]/);
});

test("CS-06 docs: statuses and argument names in the docs agree with the tool schemas", async () => {
	const manager = await startBridge("manager");
	const worker = await startBridge("worker");
	try {
		const stopCommand = manager.tools.get("stop_command")!;
		const terminal = worker.tools.get("terminal")!;
		const docs = (await Promise.all(["to-manager/references/terminal.md", "to-worker/references/task-fields.md",
			"to-worker/references/task-results.md", "workbench-recovery/SKILL.md"].map(read))).join("\n");
		for (const argument of Object.keys(stopCommand.parameters.properties)) assert.ok(docs.includes(`\`${argument}\``), argument);
		assert.ok(docs.includes("`stop`") && "stop" in terminal.parameters.properties);
		// no invented argument names for the stop tools
		for (const invented of ["stop_id", "command_ids", "`force`", "`signal:", "`grace`", "timeout_seconds"]) {
			assert.ok(!docs.includes(invented), `docs must not mention ${invented}`);
		}
		// the tool descriptions and the docs name the same statuses
		for (const status of SPEC_STATUSES) {
			assert.ok(stopCommand.description.includes(status), `stop_command description: ${status}`);
			assert.ok(terminal.description.includes(status), `terminal description: ${status}`);
			assert.ok(docs.includes(status), `docs: ${status}`);
		}
	} finally { await manager.close(); await worker.close(); }
});
