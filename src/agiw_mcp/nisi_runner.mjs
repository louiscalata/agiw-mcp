// Copyright 2026 Louis Calata
// SPDX-License-Identifier: Apache-2.0
// agiw-mcp host for exactly one Nisi v0.2.0 workflow run.
//
// agiw-mcp copies this file and the hash-verified Nisi package into a private
// temporary directory and starts it there with Node's permission model. It reads
// one JSON request on stdin and writes one JSON result on stdout. Candidate code
// is parsed by fixed checkers (node --check, Python's ast.parse, JSON.parse) and
// is never executed. The only network destinations are the configured loopback
// chat endpoints (author, and the reviewer's if it differs), enforced by Nisi's
// local-chat adapter.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { runWorkflow } from './nisi/package/workflow/engine.mjs';
import { createLocalChatAuthorAdapter, createLocalChatReviewerAdapter } from './nisi/package/adapters/local-chat.mjs';

const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
const MESSAGE_LIMIT = 400;
// Nisi rejects a test finding longer than 2048 characters (TESTS_FINDING_INVALID) and then blocks repair.
const FINDING_LIMIT = 2048;
const WORK_DIR = fileURLToPath(new URL('.', import.meta.url));
// The checker child gets this process's sandbox explicitly: Node versions differ in what they pass on.
const CHILD_SANDBOX = process.execArgv.filter(arg => /^--(?:experimental-permission|permission|allow-fs-read=|allow-fs-write=)/u.test(arg));
const PYTHON_CHECK = [
  'import ast, sys',
  'source = sys.stdin.buffer.read()',
  'try:',
  '    ast.parse(source)',
  'except SyntaxError as error:',
  '    sys.stdout.write("%s (line %s)" % (error.msg, error.lineno))',
  '    sys.exit(1)',
  'except (ValueError, MemoryError, RecursionError) as error:',
  '    sys.stdout.write(type(error).__name__)',
  '    sys.exit(1)',
].join('\n');

const fail = code => { const error = new Error(code); error.code = code; throw error; };
const clip = text => (text.length > MESSAGE_LIMIT ? `${text.slice(0, MESSAGE_LIMIT)}...` : text);
const boundFinding = text => (text.length > FINDING_LIMIT ? `${text.slice(0, FINDING_LIMIT - 3)}...` : text);

async function readRequest() {
  const chunks = [];
  let total = 0;
  for await (const chunk of process.stdin) {
    total += chunk.length;
    if (total > MAX_INPUT_BYTES) fail('REQUEST_TOO_LARGE');
    chunks.push(chunk);
  }
  let request;
  try { request = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { fail('REQUEST_INVALID'); }
  if (!request || typeof request !== 'object' || request.schema !== 1) fail('REQUEST_INVALID');
  return request;
}

function extension(path) {
  const base = path.split('/').pop();
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : '';
}

// The source goes to `node --check` on stdin, never as a file path. The child inherits the permission
// model, and checking a path makes Node resolve every parent folder (/tmp, /var), which the model refuses.
function nodeCheck(path, content, inputType, timeout) {
  const result = spawnSync(process.execPath, [...CHILD_SANDBOX, `--input-type=${inputType}`, '--check'], {
    cwd: WORK_DIR, env: process.env, timeout, input: content, stdio: ['pipe', 'ignore', 'pipe'], maxBuffer: 65536,
  });
  if (result.error || result.signal || result.status === null) return { status: 'UNAVAILABLE' };
  if (result.status === 0) return { status: 'PASS' };
  const rows = String(result.stderr ?? '').split('\n');
  const line = rows.map(row => /^\[stdin\]:(\d+)\s*$/u.exec(row)?.[1]).find(Boolean);
  const reason = rows.find(row => /^[A-Za-z]*Error\b/u.test(row)) ?? 'SyntaxError';
  return { status: 'FAIL', message: clip(`${path}${line ? `:${line}` : ''}: ${reason.split('[stdin]').join(path)}`) };
}

function syntaxCheck(path, content, request) {
  const ext = extension(path);
  const timeout = request.checkTimeoutMs;
  if (ext === 'json') {
    try { JSON.parse(content); return { status: 'PASS' }; }
    catch (error) { return { status: 'FAIL', message: clip(`${path}: ${error.message}`) }; }
  }
  if (ext === 'mjs') return nodeCheck(path, content, 'module', timeout);
  if (ext === 'cjs') return nodeCheck(path, content, 'commonjs', timeout);
  if (ext === 'js') {
    const asScript = nodeCheck(path, content, 'commonjs', timeout);
    if (asScript.status !== 'FAIL') return asScript;
    const asModule = nodeCheck(path, content, 'module', timeout);
    return asModule.status === 'FAIL' ? asScript : asModule;
  }
  if (ext === 'py') {
    if (typeof request.python !== 'string' || request.python === '') return { status: 'NOT_SUPPORTED' };
    const result = spawnSync(request.python, ['-I', '-S', '-B', '-c', PYTHON_CHECK], {
      cwd: WORK_DIR, env: process.env, input: content, timeout, stdio: ['pipe', 'pipe', 'ignore'], maxBuffer: 65536,
    });
    if (result.error || result.signal || result.status === null) return { status: 'UNAVAILABLE' };
    if (result.status === 0) return { status: 'PASS' };
    if (result.status === 1) return { status: 'FAIL', message: clip(`${path}: ${String(result.stdout ?? '')}`) };
    return { status: 'UNAVAILABLE' };
  }
  return { status: 'NOT_SUPPORTED' };
}

// Adds reasoning_effort when configured and refuses reasoning-only answers without promoting hidden
// reasoning to content. Inspect the same capped stream Nisi reads; do not clone or eagerly buffer it.
// Nisi's receipts hash the body it built; the run result reports the added field separately.
// LM Studio reads the top-level field. llama-server ignores that and reads chat_template_kwargs.
function withReasoningEffort(effort, onReasoningOnly) {
  const configured = effort !== null && effort !== undefined;
  if (configured && !['none', 'low', 'medium', 'high'].includes(effort)) fail('REQUEST_INVALID');
  return async (url, init) => {
    if (configured) {
      const body = JSON.parse(init.body);
      const template = body.chat_template_kwargs;
      const chatTemplateKwargs = template !== null && typeof template === 'object' && !Array.isArray(template)
        ? template : {};
      init = { ...init, body: JSON.stringify({
        ...body,
        reasoning_effort: effort,
        chat_template_kwargs: { ...chatTemplateKwargs, reasoning_effort: effort },
      }) };
    }
    const response = await globalThis.fetch(url, init);
    if (!response.ok || !response.body?.getReader) return response;
    const chunks = [];
    let total = 0;
    const inspected = response.body.pipeThrough(new TransformStream({
      transform(chunk, controller) {
        total += chunk.byteLength;
        if (total <= MAX_RESPONSE_BYTES) chunks.push(chunk);
        else chunks.length = 0;
        controller.enqueue(chunk);
      },
      flush() {
        if (total > MAX_RESPONSE_BYTES) return; // Nisi owns the oversized-response refusal.
        let envelope;
        try {
          envelope = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
        } catch { return; } // Leave malformed JSON and UTF-8 to Nisi's strict parser.
        const message = envelope?.choices?.[0]?.message;
        if ((message?.content == null || (typeof message.content === 'string' && message.content.trim() === ''))
            && typeof message?.reasoning_content === 'string' && message.reasoning_content.trim() !== '') {
          onReasoningOnly();
          fail('MODEL_ANSWER_IN_REASONING');
        }
      },
    }));
    return new Response(inspected, { status: response.status, statusText: response.statusText, headers: response.headers });
  };
}

function buildAdapters(request, onReasoningOnly) {
  const base = { destination: 'LOOPBACK_HTTP', endpoint: request.endpoint, timeoutMs: request.deadlineMs,
    maxOutputTokens: request.maxOutputTokens, maxRequestBytes: 8 * 1024 * 1024, maxResponseBytes: MAX_RESPONSE_BYTES };
  const innerAuthor = createLocalChatAuthorAdapter({ ...base, model: request.authorModel, id: 'agiw.author',
    fetch: withReasoningEffort(request.reasoningEffort, () => onReasoningOnly('author')) });
  // reviewerEndpoint lets the reviewer use a separate loopback server; it falls back to the author's endpoint.
  const reviewerEndpoint = typeof request.reviewerEndpoint === 'string' && request.reviewerEndpoint
    ? request.reviewerEndpoint : request.endpoint;
  const reviewer = createLocalChatReviewerAdapter({ ...base, endpoint: reviewerEndpoint, model: request.reviewerModel, id: 'agiw.reviewer',
    fetch: withReasoningEffort(request.reasoningEffort, () => onReasoningOnly('reviewer')) });
  // Nisi's task record has a fixed schema, so the request text and current file
  // contents travel to the models as extra fields on the prompt's task object.
  const context = Object.freeze({ request: request.taskText, currentFiles: request.files });
  const withContext = payload => ({ ...payload, task: { ...payload.task, ...context } });
  const author = Object.freeze({
    id: innerAuthor.id,
    draft: payload => innerAuthor.draft(withContext(payload)),
    repair: payload => innerAuthor.repair(withContext(payload)),
  });
  const reviewerWithContext = Object.freeze({ id: reviewer.id, review: payload => reviewer.review(withContext(payload)) });
  return { author, innerAuthor, reviewer, reviewerWithContext };
}

async function main() {
  const request = await readRequest();
  const original = new Map(request.files.map(file => [file.path, file.content]));
  const syntaxByCandidate = {};
  let reasoningOnlyRole = null;
  const { author, innerAuthor, reviewer, reviewerWithContext } = buildAdapters(request, role => {
    reasoningOnlyRole ??= role;
  });
  const task = {
    taskId: 'agiw-mcp.run', mode: 'edit', language: request.language, allowedFiles: request.allowedFiles,
    protectedFiles: [], protectedSnapshots: {}, acceptanceCriteria: request.acceptanceCriteria,
    policy: { repairBudget: request.repairBudget, totalDeadlineMs: request.deadlineMs, requiredReviewers: 1,
      requireReportStore: false },
  };
  const adapters = {
    authorizeContext: { authorize: ({ binding }) => ({ status: 'PASS', evidence: { ...binding, reason: '' } }) },
    author,
    staticChecks: { check({ candidate, binding }) {
      const findings = [];
      const statuses = {};
      let unavailable = false;
      for (const file of candidate.files) {
        if (file.content.includes('\u0000')) {
          findings.push({ code: 'NUL_CHARACTER', message: `${file.path} contains a NUL character.` });
          statuses[file.path] = 'FAIL';
          continue;
        }
        const result = syntaxCheck(file.path, file.content, request);
        statuses[file.path] = result.status;
        if (result.status === 'FAIL') findings.push({ code: 'SYNTAX_ERROR', message: result.message });
        if (result.status === 'UNAVAILABLE') unavailable = true;
      }
      syntaxByCandidate[binding.candidateFingerprint] = statuses;
      if (unavailable && findings.length === 0) {
        return { status: 'UNAVAILABLE', evidence: { ...binding, findings: [], reason: 'A syntax checker could not run.' } };
      }
      return { status: findings.length ? 'FAIL' : 'PASS',
        evidence: { ...binding, findings, reason: findings.length ? 'Syntax or content checks failed.' : '' } };
    } },
    tests: { run({ candidate, binding }) {
      const proposed = new Map(candidate.files.map(file => [file.path, file.content]));
      const failures = [];
      let executed = 0;
      const assert = (ok, code, message) => {
        executed += 1;
        if (!ok) failures.push({ code, message: boundFinding(message) });
      };
      assert(candidate.files.some(file => original.get(file.path) !== file.content), 'NO_CHANGE',
        'The candidate does not change any file.');
      const empty = candidate.files.filter(file => file.content.trim() === '').map(file => file.path);
      assert(empty.length === 0, 'EMPTY_FILE', `Empty proposed files: ${empty.join(', ')}.`);
      request.assertions.forEach((item, index) => {
        const content = proposed.has(item.path) ? proposed.get(item.path) : original.get(item.path);
        const code = `ASSERTION_${index + 1}`;
        if (content === undefined) { assert(false, code, `${item.path} does not exist in the result.`); return; }
        if (item.contains !== undefined) {
          assert(content.includes(item.contains), code, `${item.path} must contain the text given in assertion ${index + 1}.`);
        } else {
          assert(!content.includes(item.not_contains), code, `${item.path} must not contain the text given in assertion ${index + 1}.`);
        }
      });
      return { status: failures.length ? 'FAIL' : 'PASS', evidence: { ...binding, assertionsExecuted: executed,
        assertionsPassed: executed - failures.length, failures, reason: failures.length ? 'Acceptance assertions failed.' : '' } };
    } },
    reviewers: [reviewerWithContext],
  };
  const report = await runWorkflow(task, { adapters });
  // The pinned engine converts adapter exceptions to ADAPTER_EXCEPTION. Preserve our specific
  // refusal outside that report, identifying only the configured role, never provider text.
  if (reasoningOnlyRole !== null) {
    process.stdout.write(JSON.stringify({ kind: 'agiw-mcp.nisi-run.v1',
      error: 'MODEL_ANSWER_IN_REASONING', modelRole: reasoningOnlyRole }));
    process.exitCode = 2;
    return;
  }
  const output = {
    kind: 'agiw-mcp.nisi-run.v1',
    report,
    receipts: { author: innerAuthor.receipts(), reviewer: reviewer.receipts() },
    syntax: syntaxByCandidate[report.candidateFingerprint] ?? {},
  };
  process.stdout.write(JSON.stringify(output));
}

main().catch(error => {
  process.stdout.write(JSON.stringify({ kind: 'agiw-mcp.nisi-run.v1', error: typeof error?.code === 'string' ? error.code : 'RUNNER_FAILED' }));
  process.exitCode = 2;
});
