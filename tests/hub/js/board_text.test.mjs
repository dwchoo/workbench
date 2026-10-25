// Korean (Hangul) text and clipboard on the Board page (static/board-state.js, seam T10; no DOM, no network).
// Expected values come from the UTF-8 encoding of the test's own strings (TextEncoder / Buffer), SPEC
// "입력 한도" (a WebSocket input frame carries at most 64 KiB) and the frame layout of SPEC "WebSocket terminal
// 프로토콜", not from the module under test.
import test from "node:test";
import assert from "node:assert/strict";
import * as board from "../../../src/workbench/hub/web/static/board-state.js";

const FRAME_LIMIT = 64 * 1024;
const ATTACHED = {
  t: "attached", v: 1, run: "run-k", epoch: 3, heartbeat: 10,
  channels: { agent: { state: "running" }, shell: { state: "running" } },
};

function attachedTab() {
  let state = board.initialState({ csrf: "c" });
  const send = (event) => {
    const result = board.step(state, event);
    state = result.state;
    return result.effects;
  };
  send({ type: "start", sizes: {} });
  send({ type: "opened", conn: 1 });
  send({ type: "message", conn: 1, message: ATTACHED });
  return { send, get state() { return state; } };
}

const sent = (effects) => effects.filter((item) => item.type === "send_binary").map((item) => item.bytes);
const payloadOf = (bytes) => bytes.subarray(5);
const concat = (parts) => Buffer.concat(parts.map((part) => Buffer.from(part)));
// A UTF-8 decode that throws on any invalid or cut sequence.
const strictDecode = (bytes) => new TextDecoder("utf-8", { fatal: true }).decode(bytes);

test("typed Hangul syllables, compatibility jamo, conjoining jamo and emoji reach the frame as exact UTF-8", () => {
  const tab = attachedTab();
  const text = "안녕하세요 ㄱㄴㅎㅏ 한 👍🏽 한글";
  const frames = sent(tab.send({ type: "input", channel: "agent", bytes: new TextEncoder().encode(text) }));
  assert.equal(frames.length, 1);
  assert.equal(frames[0][0], 0);
  assert.equal(new DataView(frames[0].buffer, frames[0].byteOffset).getUint32(1, false), 3);
  assert.deepEqual(Buffer.from(payloadOf(frames[0])), Buffer.from(text, "utf8"));
});

test("a 1 MB Hangul, emoji and ASCII paste becomes in-order frames of at most 64 KiB, each valid UTF-8", () => {
  const tab = attachedTab();
  const unit = "가나다라마바사 abc 😀 \r";
  const text = unit.repeat(Math.ceil((1 << 20) / Buffer.byteLength(unit)));
  const bytes = new TextEncoder().encode(text);
  const frames = sent(tab.send({ type: "input", channel: "shell", bytes }));
  assert.ok(frames.length >= 16, `${frames.length} frames`);
  for (const frame of frames) {
    assert.equal(frame[0], 1);
    assert.equal(new DataView(frame.buffer, frame.byteOffset).getUint32(1, false), 3);
    assert.ok(payloadOf(frame).length <= FRAME_LIMIT, `payload ${payloadOf(frame).length}`);
    assert.ok(payloadOf(frame).length > FRAME_LIMIT - 4, "frames are filled up to the limit");
    strictDecode(payloadOf(frame)); // a cut code point would throw here
  }
  assert.deepEqual(concat(frames.map(payloadOf)), Buffer.from(text, "utf8"));
});

test("a split never falls inside a code point, at every offset of a 3- and 4-byte sequence at the limit", () => {
  for (const wide of ["한", "😀"]) {
    for (let pad = 0; pad < 4; pad += 1) {
      const text = "a".repeat(FRAME_LIMIT - pad) + wide + "끝";
      const tab = attachedTab();
      const frames = sent(tab.send({ type: "input", channel: "agent", bytes: new TextEncoder().encode(text) }));
      assert.equal(frames.length, 2, `${wide} pad ${pad}`);
      assert.equal(strictDecode(payloadOf(frames[0])) + strictDecode(payloadOf(frames[1])), text);
    }
  }
});

test("one input beyond the Hub's 4 MiB PTY input queue is refused whole with a notice, never sent in part", () => {
  const tab = attachedTab();
  const fits = new Uint8Array(4 << 20).fill(0x61);
  assert.equal(sent(tab.send({ type: "input", channel: "shell", bytes: fits })).length, 64);
  const effects = tab.send({ type: "input", channel: "shell", bytes: new Uint8Array((4 << 20) + 1).fill(0x61) });
  assert.deepEqual(effects.map((item) => item.type), ["notice"], "nothing is sent");
  assert.deepEqual(effects[0], { type: "notice", reason: "too_large", channel: "shell" });
});
