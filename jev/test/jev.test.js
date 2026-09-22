import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createClient, choice, scale, bool, encodeQuestions, decodeAnswers, JevError, JevTimeoutError, GATEWAY_URL, TYPESAFE_URL } from '../lib/jev.js';

const questions = {
  service: choice('which service', { repair: 'broken', install: 'new unit' }),
  size: scale('how big', ['small', 'medium', 'large']),
  real: bool('is it real'),
};

test('encodes questions for the gateway (boolean) and the direct API (noul)', () => {
  const g = encodeQuestions(questions, 'gateway');
  assert.equal(g.service.type, 'choice');
  assert.deepEqual(g.service.criteria, { repair: 'broken', install: 'new unit' });
  assert.equal(g.size.type, 'score');
  assert.deepEqual(g.size.criteria, ['small', 'medium', 'large']);
  assert.equal(g.real.type, 'boolean');
  const t = encodeQuestions(questions, 'typesafe');
  assert.equal(t.real.type, 'noul');
});

test('decodes gateway and typesafe answer shapes into one form', () => {
  const gateway = decodeAnswers(questions, {
    service: { type: 'choice', choice: 'repair', probabilities: { repair: 0.97, install: 0.03 } },
    size: { type: 'score', score: 1.2, probabilities: { 0: 0.1, 1: 0.7, 2: 0.2 } },
    real: { type: 'boolean', probability: 0.9 },
  });
  assert.equal(gateway.service.choice, 'repair');
  assert.equal(gateway.service.probability, 0.97);
  assert.equal(gateway.size.level, 'medium');
  assert.equal(gateway.size.probability, 0.7);
  assert.equal(gateway.real.value, true);

  const typesafe = decodeAnswers(questions, {
    service: { type: 'choice', choice: 'install', confidence: 0.8, probabilities: { repair: 0.2, install: 0.8 } },
    size: { type: 'score', score: 1.9 }, // no distribution: nearest level
    real: { type: 'noul', noul: 0.2 },
  });
  assert.equal(typesafe.service.choice, 'install');
  assert.equal(typesafe.size.level, 'large');
  assert.equal(typesafe.real.value, false);
  assert.equal(typesafe.real.probability, 0.2);
});

function fakeFetch(handler) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    return handler(url, init);
  };
  return { fetch, calls };
}
const okResponse = (body) => ({ ok: true, status: 200, text: async () => JSON.stringify(body) });

test('gateway route: url, headers and body match the AI SDK evaluation endpoint', async () => {
  const { fetch, calls } = fakeFetch(() => okResponse({
    answers: { service: { type: 'choice', choice: 'repair', probabilities: { repair: 0.97, install: 0.03 } }, size: { type: 'score', score: 0.4, probabilities: { 0: 0.6, 1: 0.3, 2: 0.1 } }, real: { type: 'boolean', probability: 0.95 } },
    usage: { inputTokens: 100, outputTokens: 0 },
  }));
  const client = createClient({ env: { AI_GATEWAY_API_KEY: 'k' }, fetch });
  const r = await client.decide('ac blowing warm air', questions);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, GATEWAY_URL);
  assert.equal(calls[0].init.headers.Authorization, 'Bearer k');
  assert.equal(calls[0].init.headers['ai-model-id'], 'typesafe-ai/jev');
  assert.equal(calls[0].init.headers['ai-evaluation-model-specification-version'], '4');
  assert.deepEqual(Object.keys(calls[0].body), ['state', 'questions']);
  assert.equal(calls[0].body.questions.real.type, 'boolean');
  assert.equal(r.answers.service.choice, 'repair');
  assert.equal(r.usage.inputTokens, 100);
  assert.ok(r.usage.estimatedUsd > 0 && r.usage.estimatedUsd < 0.00001);
  assert.ok(typeof r.ms === 'number');
});

test('typesafe route: /v1/systemone with model in the body and noul booleans', async () => {
  const { fetch, calls } = fakeFetch(() => okResponse({
    model: 'jev-1.13.0',
    answers: { service: { type: 'choice', choice: 'repair', confidence: 0.9, probabilities: { repair: 0.9, install: 0.1 } }, size: { type: 'score', score: 0, probabilities: { 0: 1, 1: 0, 2: 0 } }, real: { type: 'noul', noul: 0.8 } },
    usage: { input_tokens: 50, output_tokens: 0 },
  }));
  const client = createClient({ env: { TYPESAFE_API_KEY: 't' }, fetch });
  const r = await client.decide('x', questions);
  assert.equal(calls[0].url, TYPESAFE_URL);
  assert.equal(calls[0].body.model, 'jev-latest');
  assert.equal(calls[0].body.questions.real.type, 'noul');
  assert.equal(r.answers.real.probability, 0.8);
  assert.equal(r.usage.inputTokens, 50);
  assert.equal(r.model, 'jev-1.13.0');
});

test('an HTTP error is surfaced with status and body, and is not retried', async () => {
  const { fetch, calls } = fakeFetch(() => ({ ok: false, status: 401, text: async () => JSON.stringify({ error: { message: 'bad key' } }) }));
  const client = createClient({ env: { AI_GATEWAY_API_KEY: 'bad' }, fetch });
  await assert.rejects(() => client.decide('x', questions), (err) => {
    assert.ok(err instanceof JevError);
    assert.equal(err.status, 401);
    assert.deepEqual(err.body, { error: { message: 'bad key' } });
    assert.match(err.message, /401/);
    return true;
  });
  assert.equal(calls.length, 1);
});

test('a slow answer becomes JevTimeoutError', async () => {
  const fetch = (url, init) => new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  const client = createClient({ env: { AI_GATEWAY_API_KEY: 'k' }, fetch, timeoutMs: 20 });
  await assert.rejects(() => client.decide('x', questions), JevTimeoutError);
});

test('no key and no provider is a clear error; mock needs no key', () => {
  assert.throws(() => createClient({ env: {} }), /No jev provider configured/);
  const mock = createClient({ env: { JEV_PROVIDER: 'mock' } });
  assert.equal(mock.provider, 'mock');
});
