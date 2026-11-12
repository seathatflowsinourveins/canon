#!/usr/bin/env node
// Contract: Agent Client Protocol (ACP), v1 (checked 2026-11-11):
// https://agentclientprotocol.com/protocol/v1/initialization
// The spec defines the version as "a single integer that identifies a MAJOR protocol version"
// and its initialize example sends "protocolVersion": 1; this probe sends protocolVersion: 1.
// Request/response dispatch follows JSON-RPC 2.0 sections 4-5:
// https://www.jsonrpc.org/specification
// Requests carry method; responses are correlated by id only after excluding requests.
// Updates and completion use the ACP v1 sessionUpdate and stopReason fields:
// https://agentclientprotocol.com/protocol/v1/prompt-turn
// Permission selection uses the one-shot option kind, never the opaque optionId:
// https://agentclientprotocol.com/protocol/v1/tool-calls
// PermissionOption[] and SelectedPermissionOutcome require a string optionId:
// https://agentclientprotocol.com/protocol/v1/schema
// ACP stdio frames are JSON-RPC objects, not null, scalars or batches:
// https://agentclientprotocol.com/protocol/v1/transports
// Every inbound object requires jsonrpc: "2.0" (JSON-RPC 2.0 sections 4-5 above).
// Close is capability-gated; -32002 on close counts as success:
// https://agentclientprotocol.com/protocol/v1/session-setup
//
// Known limits (review 2026-11-11):
// - The negotiated protocolVersion is not validated.
// - PASS checks end_turn and optional tool activity, not the exact requested ACP-OK reply.
//   Payload validation is partial; unmatched responses are ignored.
// - Non-JSON stdout lines are counted and named in diagnostics, but do not fail the probe.
// - Child spawn/exit and stdin errors have no handlers: spawn/write errors may be unhandled,
//   and an early child exit leaves requests pending until the deadline.
//   Teardown sends SIGTERM and exits immediately, without stdin EOF, waiting for shutdown,
//   session/cancel, or cancelled outcomes for outstanding permission requests.
//   The pinned DSH app's EOF/shutdown contract is described in:
//   https://github.com/deepseek-ai/deepseek-harness/blob/1f81c047fc48fa190a683e469daa699857e9afe8/packages/bundle/acp-app/README.md
// - Empty option values fall back; a following flag can be consumed as a value.
//   --timeout is not validated; NaN, values below 1 or above 2147483647 use Node's 1 ms delay.
// - ACP v1 is registered by ADR 0095; dsh-acp-app adoption remains deferred in ADR 0086.
//   The profile inventory does not establish adoption or enabled composition.
//
// Minimal ACP v1 probe against a dsh ACP profile, with the PASS predicate documented above.
//
//   node scripts/acp-probe.mjs [--profile review-acp] [--prompt "..."]

import { spawn } from 'node:child_process';

const args = process.argv.slice(2);
const arg = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const profile = arg('profile', 'review-acp');
const prompt = arg('prompt', 'Reply with exactly: ACP-OK');
const cwd = process.cwd();
const DEADLINE_MS = Number(arg('timeout', '180000'));
const deny = args.includes('--deny');   // answer reject-once instead of allow-once

const child = spawn('dsh', ['--profile', profile], { cwd, stdio: ['pipe', 'pipe', 'pipe'] });

const events = [];
const permissionRequests = [];
// ACP v1 prompt-turn: the variants supported without clientCapabilities.session.
// notice requires session.notices; both compaction variants require session.compaction.
// https://agentclientprotocol.com/protocol/v1/prompt-turn
const sessionUpdateKinds = new Set([
  'user_message_chunk', 'agent_message_chunk', 'agent_thought_chunk',
  'tool_call', 'tool_call_update', 'plan', 'available_commands_update',
  'current_mode_update', 'config_option_update', 'session_info_update',
  'usage_update',
]);
let toolCalls = 0;
let nextId = 1;
const pending = new Map();
let buffer = '';
let assistantText = '';
let stdoutLines = 0;
let skippedNonJsonLines = 0;

const log = (...a) => console.log(...a);
const send = (method, params) => {
  const id = nextId++;
  const frame = { jsonrpc: '2.0', id, method, params };
  child.stdin.write(JSON.stringify(frame) + '\n');
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject, method });
  });
};

child.stdout.on('data', (chunk) => {
  buffer += chunk.toString('utf8');
  let nl;
  while ((nl = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, nl).trim();
    buffer = buffer.slice(nl + 1);
    stdoutLines += 1;
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch {
      skippedNonJsonLines += 1;
      log(`skipped non-JSON stdout line ${stdoutLines} (#${skippedNonJsonLines}): ${JSON.stringify(line)}`);
      continue;
    }
    if (!msg || typeof msg !== 'object' || Array.isArray(msg)) {
      return fail('ACP frame must be a JSON-RPC object');
    }
    if (msg.jsonrpc !== '2.0') return fail('ACP frame requires jsonrpc: "2.0"');

    // A peer request can reuse a pending id; only responses may settle our request.
    if (msg.method === undefined && msg.id !== undefined && pending.has(msg.id)) {
      const { resolve, reject, method } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(Object.assign(new Error(`${method}: ${JSON.stringify(msg.error)}`), {
        code: msg.error.code,
      }));
      else resolve(msg.result);
      continue;
    }
    if (msg.method === 'session/update') {
      const upd = msg.params?.update ?? {};
      const kind = upd.sessionUpdate;
      if (!sessionUpdateKinds.has(kind)) {
        return fail(`unsupported ACP v1 sessionUpdate: ${JSON.stringify(kind)}`);
      }
      events.push(kind);
      if (kind === 'tool_call' || kind === 'tool_call_update') toolCalls += 1;
      if (kind === 'agent_message_chunk') {
        const c = upd.content;
        if (!c || typeof c !== 'object' || typeof c.type !== 'string') {
          return fail('agent_message_chunk requires an ACP ContentBlock object');
        }
        if (c.type === 'text') {
          if (typeof c.text !== 'string') return fail('text ContentBlock requires text');
          assistantText += c.text;
        }
      }
      continue;
    }
    if (msg.id !== undefined && msg.method === 'session/request_permission') {
      // ACP v1: pick an option and answer with a structured outcome. Answering `result: null` here
      // would make the first permission-gated tool call fail or hang.
      // ACP v1 schema: options is PermissionOption[], and the selected optionId is a string.
      // https://agentclientprotocol.com/protocol/v1/schema
      const options = msg.params?.options;
      if (!Array.isArray(options) || options.some((o) => !o || typeof o !== 'object' || Array.isArray(o))) {
        return fail('session/request_permission requires an array of PermissionOption objects');
      }
      const wantAllow = !deny;
      const pick = options.find((o) => o.kind === (wantAllow ? 'allow_once' : 'reject_once'));
      // No fallback: an arm that cannot find its own kind must fail loudly. Answering options[0] could
      // answer an allow in the deny arm, and the missing file would then be credited to the gate.
      if (!pick) {
        const permissionError = `--${wantAllow ? 'allow' : 'deny'} arm: server offered no ${wantAllow ? 'allow' : 'reject'} option; `
          + `options were ${JSON.stringify(options.map((o) => ({ kind: o.kind, optionId: o.optionId })))}`;
        child.kill('SIGTERM');
        return fail(permissionError);
      }
      if (typeof pick.optionId !== 'string') {
        return fail('selected PermissionOption requires a string optionId');
      }
      const reply = { outcome: { outcome: 'selected', optionId: pick.optionId } };
      const answeredKind = deny ? 'reject' : 'allow';
      permissionRequests.push({ arm: answeredKind, requested: options.map((o) => o.optionId), answered: reply.outcome, optionKind: pick.kind });
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: msg.id, result: reply }) + '\n');
      continue;
    }
    if (msg.method !== undefined && msg.id !== undefined) {
      // Refuse unimplemented requests; unmatched responses receive no reply.
      child.stdin.write(JSON.stringify({
        jsonrpc: '2.0', id: msg.id,
        error: { code: -32601, message: `client does not implement ${msg.method}` },
      }) + '\n');
    }
  }
});

child.stderr.on('data', (d) => process.stderr.write(d));

const fail = (why) => {
  log(`\nPROBE FAILED: ${why}`);
  log(`update kinds seen: ${[...new Set(events)].join(', ') || '(none)'}`);
  log(`skipped non-JSON stdout lines: ${skippedNonJsonLines}`);
  child.kill('SIGTERM');
  process.exit(1);
};

const timer = setTimeout(() => fail(`no completion within ${DEADLINE_MS} ms`), DEADLINE_MS);

(async () => {
  try {
    const init = await send('initialize', {
      protocolVersion: 1,
      clientCapabilities: { fs: { readTextFile: false, writeTextFile: false } },
    });
    log('1. initialize ok — capabilities:',
        Object.keys(init?.agentCapabilities ?? init ?? {}).join(', ') || '(none)');

    const session = await send('session/new', { cwd, mcpServers: [] });
    const sessionId = session?.sessionId;
    if (!sessionId) fail('session/new returned no session id');
    log(`2. session/new ok — sessionId=${sessionId}`);

    const options = session?.configOptions;
    if (Array.isArray(options) && options.length) {
      log('   config options:', options.map((o) => o.id ?? o.name).join(', '));
    }

    log(`3. session/prompt → "${prompt}"`);
    const result = await send('session/prompt', {
      sessionId,
      prompt: [{ type: 'text', text: prompt }],
    });
    log(`   stopReason=${result?.stopReason ?? '(none)'}`);
    log(`   assistant text: ${JSON.stringify(assistantText.trim().slice(0, 200))}`);
    log(`   update kinds: ${[...new Set(events)].join(', ') || '(none)'}`);
    log(`   tool updates: ${toolCalls}; permission requests: ${permissionRequests.length ? JSON.stringify(permissionRequests) : 'none'}`);
    log(`   skipped non-JSON stdout lines: ${skippedNonJsonLines}`);

    // ACP v1 session-setup: omit close unless advertised; unknown session is already closed.
    // https://agentclientprotocol.com/protocol/v1/session-setup
    if (init?.agentCapabilities?.sessionCapabilities?.close != null) {
      try {
        await send('session/close', { sessionId });
      } catch (error) {
        if (error.code !== -32002) throw error;
      }
      log('4. session/close ok');
    } else {
      log('4. session/close skipped (not advertised)');
    }

    clearTimeout(timer);
    const requireTool = args.includes('--require-tool');
    const failure = result?.stopReason !== 'end_turn'
      ? `expected stopReason=end_turn, got ${JSON.stringify(result?.stopReason) ?? '(missing)'}`
      : requireTool && toolCalls === 0 ? 'no tool call was observed (--require-tool)' : null;
    log(failure ? `\nPROBE FAILED: ${failure}` : '\nPROBE PASSED');
    child.kill('SIGTERM');
    process.exit(failure ? 1 : 0);
  } catch (error) {
    fail(error.message);
  }
})();
