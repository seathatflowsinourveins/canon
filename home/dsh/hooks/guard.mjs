#!/usr/bin/env node
/**
 * Canon guard — PreToolUse hook for the DeepSeek Harness.
 *
 * dsh-tui workers run workspace-write with approval never, so a wider-access
 * request is refused rather than prompted (ADR 0136), and the desktop profile's
 * own patch says its sessions reject approvals as well. With no approval in the
 * way, a Claude Code PreToolUse hook is still a real veto: dsh maps its deny to
 * a tools/pre-execute refusal.
 *
 * It is a tripwire for a few irreversible commands, not a control. Tried on
 * 2026-11-13, it also denies ordinary deletes under the home folder
 * (rm -rf ~/.cache/foo), and it lets through: rm -rf "$HOME" or ${HOME}; a
 * force push written as +ref or as -f; (a semicolon after the flag), or one
 * followed on the line by --force-with-lease; a pipe into /bin/sh or
 * sudo -E bash, and bash <(curl ...); a shell command that reads a secret
 * path, since SECRET_PATHS guard the file tools only; a write to ~/.SSH or
 * /private/etc, since a path is compared as written, with no case folding and
 * no symlink resolved; and a terminal_send split over sends with submit false.
 * Two more points a review raised were not reproduced: that deny() exits
 * before a long write to a pipe has flushed, and that an unset HOME leaves
 * SECRET_PATHS relative.
 *
 * The guard fails open: every parse or failure path exits 0 with no output,
 * and a hook that fails to run is logged while the agent continues
 * (README.md L68-75). SECRET_PATHS derive from HOME: each entry is
 * path.join(HOME, ...) except the fixed /etc, /System and /usr.
 *
 * Contract (dsh-hook-protocol): exit 0 with
 *   {"hookSpecificOutput":{"hookEventName":"PreToolUse",
 *    "permissionDecision":"deny","permissionDecisionReason":"..."}}
 * denies the call with a model-visible reason. The matcher subject is the tool
 * name, and deny > ask > allow (README.md L62, L97); a provider reason passes
 * through verbatim (L158), and PreToolUse is partial: deny and ask work (L178).
 *
 * Upstream, read at deepseek-harness pin ddac96760b6e (dsh-v0.2.2-rc.2); each
 * payload field and tool name below reads as it did at 1f81c047fc48:
 *   packages/experimental/hooks-claude-code/README.md L48 (configPath), L93
 *   (payload base), L62 (PreToolUse), L68-75, L97, L158, L178;
 *   packages/experimental/hooks-claude-code/src/index.ts L333, L344 (the
 *   PreToolUse payload fields); packages/experimental/hook-protocol/src/
 *   runner.ts L2-3, L87 (a command hook runs through ctx.shell);
 *   packages/shell/tool-bash/src/index.ts L393, L403 (bash, command);
 *   packages/shell/tool-pwsh/src/index.ts L409, L420 (pwsh, command);
 *   packages/experimental/tool-terminal/src/index.ts L203, L208 (terminal_send,
 *   text); packages/fs/tool-fs/src/write.ts L74, L77 (write, file_path);
 *   packages/fs/tool-fs/src/edit.ts L86, L89 (edit, file_path);
 *   packages/fs/tool-str-replace-editor/src/index.ts L435, L444
 *   (str_replace_editor, path).
 * The matcher's terminal and multi_edit name no tool at this pin; they stay
 * because the owner's hooks.json has them. No worker profile loads the
 * terminal tools at this pin (home/dsh/profiles.json), so terminal_send applies
 * only where they still load: the desktop profile, which carries the terminal
 * bundle beside the Web bundle that composes its controller (ADR 0143;
 * packages/bundle/web-app/cordis.patch.yml L126-127).
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';

const HOME = process.env.HOME ?? '';

function deny(reason) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: reason,
    },
  }));
  process.exit(0);
}

function readPayload() {
  try {
    const raw = readFileSync(0, 'utf8').trim();
    if (!raw) return undefined;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : undefined;
  } catch {
    return undefined;
  }
}

function normalizeTarget(raw, cwd) {
  let p = String(raw ?? '').trim();
  if (!p) return '';
  if (p === '~') p = HOME;
  else if (p.startsWith('~/')) p = path.join(HOME, p.slice(2));
  if (!path.isAbsolute(p)) p = path.resolve(cwd || process.cwd(), p);
  return path.normalize(p);
}

const BASH_RULES = [
  {
    re: /\brm\s+(?=[^\n;&|]*-(?:[a-zA-Z]*[rR][a-zA-Z]*[fF]?|[a-zA-Z]*[fF][a-zA-Z]*[rR]))[^\n;&|]*\s(?:(?:~|\$HOME)(?=\/|\s|$|\*)|\/(?=\s|$|\*))/,
    why: 'recursive delete of / or $HOME',
  },
  { re: /\b(?:mkfs(?:\.[a-z0-9]+)?|newfs_[a-z0-9]+)\b/, why: 'filesystem format' },
  { re: /\bdiskutil\s+erase\w*\b/, why: 'disk erase' },
  { re: /\bdd\b[^\n]*\bof=\/dev\/(?:disk|rdisk|sd|nvme|hd|mmcblk|vd|xvd|fd)/, why: 'raw write to a block device' },
  { re: /:\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:/, why: 'fork bomb' },
  { re: /\b(?:curl|wget)\b[^\n|;]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|fish)\b/, why: 'download-and-execute pipeline' },
  { re: /\bsudo\s+rm\b/, why: 'privileged delete' },
  { re: /\bgit\s+push\b(?![^\n]*--force-with-lease)[^\n]*\s(?:--force|-f)(?=\s|$)/, why: 'force push without --force-with-lease' },
  { re: /\bchmod\s+-R\s+777\s+\/(?=\s|$)/, why: 'world-writable root' },
];

const SECRET_PATHS = [
  path.join(HOME, '.ssh'),
  path.join(HOME, '.aws'),
  path.join(HOME, '.netrc'),
  path.join(HOME, '.dsh', '.credentials.yaml'),
  path.join(HOME, '.config', 'gh', 'hosts.yml'),
  path.join(HOME, '.config', 'gcloud'),
  '/etc',
  '/System',
  '/usr',
];

function under(target, root) {
  return target === root || target.startsWith(root + path.sep);
}

const payload = readPayload();
if (payload) {
  const tool = String(payload.tool_name ?? payload.toolName ?? '');
  const input = payload.tool_input ?? payload.toolInput ?? {};
  const cwd = String(payload.cwd ?? process.cwd());

  if (tool === 'bash' || tool === 'terminal' || tool === 'terminal_send' || tool === 'pwsh') {
    const command = String(input.command ?? input.cmd ?? input.script ?? input.text ?? '');
    for (const rule of BASH_RULES) {
      if (rule.re.test(command)) {
        deny(`Blocked by canon guard: ${rule.why}. Nothing ran. If this is genuinely required, say so explicitly and let the operator run it.`);
      }
    }
  }

  if (tool === 'write' || tool === 'edit' || tool === 'multi_edit' || tool === 'str_replace_editor') {
    const raw = input.file_path ?? input.path ?? input.filePath ?? input.target ?? '';
    const target = normalizeTarget(raw, cwd);
    for (const secret of SECRET_PATHS) {
      if (target && under(target, secret)) {
        deny(`Blocked by canon guard: refusing to write ${target} (credential/system path). Use the operator's own tooling for this file.`);
      }
    }
  }
}

process.exit(0);
