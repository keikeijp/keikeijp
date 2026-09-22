import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classifyAction, judge, buildState, buildQuestions, createGuard, DEFAULT_RULES } from '../guard/guard.js';
import { createClient } from '../lib/jev.js';

const B = (p) => ({ type: 'bool', probability: p, value: p >= 0.5 });
const spend = (choice, p) => ({ type: 'choice', choice, probability: p, probabilities: { approve: choice === 'approve' ? p : (1 - p) / 2, review: choice === 'review' ? p : (1 - p) / 2, deny: choice === 'deny' ? p : (1 - p) / 2 } });
const sure = (over = {}) => ({ duplicate: B(0.05), off_goal: B(0.05), spend: spend('approve', 0.95), reversible: B(0.9), last_step_worked: B(0.95), ...over });

test('classifyAction: sensitive by type or verb, cheap by cost, else normal', () => {
  assert.equal(classifyAction({ type: 'email', cost_usd: 0 }), 'sensitive');
  assert.equal(classifyAction({ type: 'task', description: 'Delete the leads database', cost_usd: 0 }), 'sensitive');
  assert.equal(classifyAction({ type: 'render', cost_usd: 0.8 }), 'cheap');
  assert.equal(classifyAction({ type: 'render', cost_usd: 400 }), 'normal');
});

test('a deny always stops it, whatever the numbers say', () => {
  const v = judge(sure({ spend: spend('deny', 0.51) }), 'cheap');
  assert.equal(v.outcome, 'deny');
});

test('thresholds by class: 0.8 normal, 0.7 cheap+reversible, 0.9 sensitive', () => {
  assert.equal(judge(sure({ duplicate: B(0.25) }), 'normal').outcome, 'hold'); // 0.75 < 0.8
  assert.equal(judge(sure({ duplicate: B(0.25) }), 'cheap').outcome, 'run'); // 0.75 >= 0.7
  assert.equal(judge(sure({ duplicate: B(0.25), reversible: B(0.4) }), 'cheap').outcome, 'hold'); // cheap but not reversible -> 0.8
  assert.equal(judge(sure({ spend: spend('approve', 0.85) }), 'sensitive').outcome, 'hold'); // 0.85 < 0.9
  assert.equal(judge(sure({ spend: spend('approve', 0.95) }), 'sensitive').outcome, 'run');
  // normal action jev is 90%+ sure cannot be undone is held to the sensitive bar
  assert.equal(judge(sure({ reversible: B(0.05), spend: spend('approve', 0.85) }), 'normal').outcome, 'hold');
  assert.equal(judge(sure({ spend: spend('review', 0.6) }), 'normal').outcome, 'hold');
  assert.equal(judge(sure({ last_step_worked: B(0.07) }), 'normal').outcome, 'hold');
  assert.equal(judge(sure({ last_step_worked: B(0.07) }), 'normal', DEFAULT_RULES, { hasHistory: false }).outcome, 'run');
  assert.equal(judge(sure({ lead_real: B(0.3) }), 'normal', DEFAULT_RULES, { hasLead: true }).outcome, 'hold');
});

test('state carries the last ten actions and the full lead thread; lead adds the sixth question', () => {
  const history = Array.from({ length: 14 }, (_, i) => ({ type: 'log', description: `step ${i}`, status: 'ok' }));
  const thread = [{ from: 'a', text: 'hi' }, { from: 'me', text: 'hello' }];
  const state = buildState({ type: 'email', description: 'reply', cost_usd: 0, lead: { thread } }, { goal: 'g', history });
  assert.equal(state.last_actions.length, 10);
  assert.equal(state.last_actions[0].description, 'step 4');
  assert.deepEqual(state.thread, thread);
  assert.deepEqual(Object.keys(buildQuestions({ hasLead: true })), ['duplicate', 'off_goal', 'spend', 'reversible', 'last_step_worked', 'lead_real']);
  assert.deepEqual(Object.keys(buildQuestions({ hasLead: false })), ['duplicate', 'off_goal', 'spend', 'reversible', 'last_step_worked']);
  assert.deepEqual(Object.keys(buildQuestions({ hasLead: true, lite: true })), ['duplicate', 'off_goal']);
});

test('the budget is counted, not asked: over budget holds before any jev call', async () => {
  let calls = 0;
  const client = { provider: 'fake', decide: async () => { calls += 1; return { answers: sure(), ms: 1 }; } };
  const guard = createGuard({ client, rules: { budgetUsd: 100 } });
  const held = await guard.check({ type: 'ads', description: 'run ads', cost_usd: 120 }, { goal: 'g', history: [], spentTodayUsd: 0 });
  assert.equal(held.outcome, 'hold');
  assert.match(held.reason, /budget/);
  assert.equal(calls, 0);
  const ran = await guard.check({ type: 'ads', description: 'run ads', cost_usd: 20 }, { goal: 'g', history: [], spentTodayUsd: 79 });
  assert.equal(ran.outcome, 'run');
  assert.equal(calls, 1);
});

test('a held or denied previous step does not block the next one (a person is already in the loop)', async () => {
  const seen = [];
  const client = { provider: 'fake', decide: async (state) => { seen.push(state); return { answers: sure({ last_step_worked: B(0.5) }), ms: 1 }; } };
  const guard = createGuard({ client });
  const held = await guard.check({ type: 'log', description: 'x', cost_usd: 0 }, { goal: 'g', history: [{ type: 'email', description: 'e', status: 'bounced' }] });
  assert.equal(held.outcome, 'hold');
  const ran = await guard.check({ type: 'log', description: 'x', cost_usd: 0 }, { goal: 'g', history: [{ type: 'email', description: 'e', status: 'bounced' }, { type: 'email', description: 'retry', status: 'held' }] });
  assert.equal(ran.outcome, 'run');
  assert.equal(seen[1].last_actions.length, 2); // the history still shows both, jev sees everything
});

test('no answer within the timeout holds the action', async () => {
  const client = createClient({ env: { JEV_PROVIDER: 'mock' } });
  const guard = createGuard({ client, rules: { timeoutMs: 30 } });
  const entry = await guard.check({ type: 'log', description: 'log it', cost_usd: 0, _mockDelayMs: 60 }, { goal: 'log everything', history: [] });
  assert.equal(entry.outcome, 'hold');
  assert.match(entry.reason, /did not answer within 30ms/);
});

test('every decision is logged with answers, probabilities and outcome', async () => {
  const log = [];
  const client = { provider: 'fake', decide: async () => ({ answers: sure({ spend: spend('deny', 0.9) }), ms: 3, usage: { inputTokens: 10 } }) };
  const guard = createGuard({ client, rules: { budgetUsd: 2000 }, onLog: (e) => log.push(e) });
  await guard.check({ id: 'x', type: 'pay', description: 'pay invoice', cost_usd: 950 }, { goal: 'g', history: [{ type: 'log', description: 'a', status: 'ok' }] });
  assert.equal(log.length, 1);
  assert.equal(log[0].outcome, 'deny');
  assert.equal(log[0].answers.spend.choice, 'deny');
  assert.equal(log[0].action.id, 'x');
  assert.equal(log[0].ms, 3);
});
