// jev client. One call = one shared state + several typed questions, answered in parallel.
//
// Two real routes and one offline stand-in:
//   gateway  - Vercel AI Gateway   POST https://ai-gateway.vercel.sh/v4/ai/evaluation-model
//              (the exact request/response the AI SDK's `experimental_evaluate` sends; verified
//              against @ai-sdk/gateway 4.0.88)
//   typesafe - direct TypeSafe API POST https://api.typesafe.ai/v1/systemone
//              (verified against @typesafe-ai/sdk 0.6.0; booleans are called "noul" there)
//   mock     - deterministic keyword heuristics, for tests and offline demos. NOT jev.
//
// Failures are surfaced, never retried silently: a bad key, a 4xx/5xx body or a timeout comes
// back as a JevError with the actual status and body attached.

import { performance } from 'node:perf_hooks';
import { mockDecide } from './mock.js';

export const GATEWAY_URL = 'https://ai-gateway.vercel.sh/v4/ai/evaluation-model';
export const TYPESAFE_URL = 'https://api.typesafe.ai/v1/systemone';
export const GATEWAY_MODEL = 'typesafe-ai/jev';
export const TYPESAFE_MODEL = 'jev-latest';
export const INPUT_USD_PER_MILLION_TOKENS = 0.042; // output is free

export class JevError extends Error {
  constructor(message, { status, body, url, provider, cause } = {}) {
    super(message, cause ? { cause } : undefined);
    this.name = 'JevError';
    this.status = status;
    this.body = body;
    this.url = url;
    this.provider = provider;
  }
}
export class JevTimeoutError extends JevError {
  constructor(ms, extra) {
    super(`jev did not answer within ${ms}ms`, extra);
    this.name = 'JevTimeoutError';
    this.timeoutMs = ms;
  }
}

// ---- question builders -------------------------------------------------------------------

/** Pick one option from a list. `options` is an array of names or a map name -> description. */
export function choice(instructions, options) {
  const criteria = Array.isArray(options)
    ? Object.fromEntries(options.map((o) => [o, null]))
    : { ...options };
  if (Object.keys(criteria).length === 0) throw new JevError('choice needs at least one option');
  return { type: 'choice', instructions, criteria };
}

/** Put something on an ordered scale. `levels` is an ordered array of names or [name, description]. */
export function scale(instructions, levels) {
  const parsed = levels.map((l) => (Array.isArray(l) ? { name: l[0], description: l[1] } : { name: l, description: null }));
  if (parsed.length < 2) throw new JevError('scale needs at least two levels');
  return { type: 'scale', instructions, levels: parsed };
}

/** True or false. Optional descriptions of what true / false mean. */
export function bool(instructions, criteria) {
  return { type: 'bool', instructions, ...(criteria ? { criteria } : {}) };
}

// ---- wire encoding / decoding -------------------------------------------------------------

export function encodeQuestions(questions, provider) {
  const out = {};
  for (const [id, q] of Object.entries(questions)) {
    if (q.type === 'choice') {
      out[id] = { type: 'choice', instructions: q.instructions, criteria: q.criteria };
    } else if (q.type === 'scale') {
      // The level name is sent as its own description when none is given, so the model sees the label.
      out[id] = {
        type: 'score',
        instructions: q.instructions,
        criteria: q.levels.map((l) => (l.description ? `${l.name}: ${l.description}` : l.name)),
      };
    } else if (q.type === 'bool') {
      out[id] = {
        type: provider === 'typesafe' ? 'noul' : 'boolean',
        instructions: q.instructions,
        ...(q.criteria ? { criteria: q.criteria } : {}),
      };
    } else {
      throw new JevError(`unknown question type "${q.type}" for "${id}"`);
    }
  }
  return out;
}

function argmax(probabilities) {
  let best = null;
  for (const [k, v] of Object.entries(probabilities)) if (best === null || v > best[1]) best = [k, v];
  return best;
}

export function decodeAnswers(questions, answers) {
  const out = {};
  for (const [id, q] of Object.entries(questions)) {
    const a = answers?.[id];
    if (!a) throw new JevError(`no answer for question "${id}"`, { body: answers });
    if (q.type === 'choice') {
      const probabilities = a.probabilities ?? { [a.choice]: 1 };
      out[id] = { type: 'choice', choice: a.choice, probability: probabilities[a.choice] ?? a.confidence ?? null, probabilities };
    } else if (q.type === 'scale') {
      const names = q.levels.map((l) => l.name);
      let index;
      let probabilities;
      if (a.probabilities) {
        const [k, p] = argmax(a.probabilities);
        index = Number(k);
        probabilities = Object.fromEntries(names.map((n, i) => [n, a.probabilities[String(i)] ?? 0]));
        out[id] = { type: 'scale', level: names[index], index, probability: p, score: a.score, probabilities };
      } else {
        index = Math.min(names.length - 1, Math.max(0, Math.round(a.score)));
        // No distribution: use distance from the nearest level as a stand-in for certainty.
        const p = 1 - Math.min(1, Math.abs(a.score - index) * 2);
        out[id] = { type: 'scale', level: names[index], index, probability: p, score: a.score, probabilities: null };
      }
    } else if (q.type === 'bool') {
      const p = a.type === 'noul' ? a.noul : a.probability;
      if (typeof p !== 'number') throw new JevError(`boolean answer "${id}" has no probability`, { body: a });
      out[id] = { type: 'bool', probability: p, value: p >= 0.5 };
    }
  }
  return out;
}

function normalizeUsage(usage) {
  if (!usage) return null;
  const inputTokens = usage.inputTokens ?? usage.input_tokens ?? null;
  const outputTokens = usage.outputTokens ?? usage.output_tokens ?? null;
  return {
    inputTokens,
    outputTokens,
    estimatedUsd: inputTokens == null ? null : (inputTokens * INPUT_USD_PER_MILLION_TOKENS) / 1e6,
  };
}

// ---- client ------------------------------------------------------------------------------

export function detectProvider(env = process.env) {
  if (env.JEV_PROVIDER) return env.JEV_PROVIDER;
  if (env.AI_GATEWAY_API_KEY) return 'gateway';
  if (env.TYPESAFE_API_KEY) return 'typesafe';
  return null;
}

export function createClient(opts = {}) {
  const env = opts.env ?? process.env;
  const provider = opts.provider ?? detectProvider(env);
  if (!provider) {
    throw new JevError(
      'No jev provider configured. Put AI_GATEWAY_API_KEY (or TYPESAFE_API_KEY) in .env, or set JEV_PROVIDER=mock for the offline stand-in.',
    );
  }
  if (!['gateway', 'typesafe', 'mock'].includes(provider)) throw new JevError(`unknown JEV_PROVIDER "${provider}"`);

  const fetchImpl = opts.fetch ?? globalThis.fetch;
  const defaultTimeoutMs = opts.timeoutMs ?? 10_000;
  const model = provider === 'mock' ? 'mock' : (opts.model ?? env.JEV_MODEL ?? (provider === 'typesafe' ? TYPESAFE_MODEL : GATEWAY_MODEL));
  const apiKey = opts.apiKey ?? (provider === 'gateway' ? env.AI_GATEWAY_API_KEY : env.TYPESAFE_API_KEY);
  if (provider !== 'mock' && !apiKey) {
    throw new JevError(`${provider === 'gateway' ? 'AI_GATEWAY_API_KEY' : 'TYPESAFE_API_KEY'} is not set`);
  }
  const url = provider === 'mock' ? null : (opts.baseURL ?? (provider === 'gateway' ? GATEWAY_URL : TYPESAFE_URL));

  async function decide(state, questions, callOpts = {}) {
    if (!questions || Object.keys(questions).length === 0) throw new JevError('at least one question is required');
    const timeoutMs = callOpts.timeoutMs ?? defaultTimeoutMs;
    const wireQuestions = encodeQuestions(questions, provider);
    const t0 = performance.now();

    if (provider === 'mock') {
      const raw = mockDecide(state, wireQuestions);
      if (callOpts.mockDelayMs) await new Promise((r) => setTimeout(r, callOpts.mockDelayMs));
      const ms = performance.now() - t0;
      if (ms > timeoutMs) throw new JevTimeoutError(timeoutMs, { provider });
      return { answers: decodeAnswers(questions, raw.answers), ms, usage: normalizeUsage(raw.usage), raw, model: 'mock', provider };
    }

    const body = provider === 'gateway' ? { state, questions: wireQuestions } : { model, state, questions: wireQuestions };
    const headers = {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
      Accept: 'application/json',
      ...(provider === 'gateway' ? { 'ai-evaluation-model-specification-version': '4', 'ai-model-id': model } : {}),
    };

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    if (callOpts.signal) callOpts.signal.addEventListener('abort', () => controller.abort(), { once: true });
    let res;
    let text;
    try {
      res = await fetchImpl(url, { method: 'POST', headers, body: JSON.stringify(body), signal: controller.signal });
      text = await res.text();
    } catch (cause) {
      if (controller.signal.aborted && !callOpts.signal?.aborted) throw new JevTimeoutError(timeoutMs, { url, provider, cause });
      throw new JevError(`request to ${url} failed: ${cause?.message ?? cause}`, { url, provider, cause });
    } finally {
      clearTimeout(timer);
    }
    const ms = performance.now() - t0;
    let parsed;
    try {
      parsed = text ? JSON.parse(text) : undefined;
    } catch {
      parsed = text;
    }
    if (!res.ok) {
      throw new JevError(`${provider} returned HTTP ${res.status}: ${typeof parsed === 'string' ? parsed : JSON.stringify(parsed)}`, {
        status: res.status, body: parsed, url, provider,
      });
    }
    if (!parsed || typeof parsed !== 'object' || !parsed.answers) {
      throw new JevError(`unexpected response shape from ${url}`, { status: res.status, body: parsed, url, provider });
    }
    return { answers: decodeAnswers(questions, parsed.answers), ms, usage: normalizeUsage(parsed.usage), raw: parsed, model: parsed.model ?? model, provider };
  }

  return { provider, model, url, decide };
}
