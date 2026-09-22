#!/usr/bin/env node
// Records a replay for the dashboard: every guard check of the fake day and every quote
// demo request, with jev's answers, latency and cost. Nothing is executed or sent.
//
//   node dashboard/record.js                 # provider from .env (gateway / typesafe) or mock
//   node dashboard/record.js --race          # also time the same decision through other models
//                                            # via the AI Gateway (needs AI_GATEWAY_API_KEY)
//   node dashboard/record.js --out dashboard/replay.json
//
// The race sends the identical state + questions to a writing model as a JSON-answer prompt
// and times the round trip. It is a latency comparison only; the writing model's answers are
// not used for anything.
import { readFileSync, writeFileSync } from 'node:fs';
import { loadEnv } from '../lib/env.js';
import { createClient, encodeQuestions } from '../lib/jev.js';
import { createGuard, buildState, buildQuestions } from '../guard/guard.js';
import { createEngine, loadConfig } from '../quote/engine.js';

loadEnv();
const args = process.argv.slice(2);
const flag = (name, def) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : def; };
const out = flag('out', 'dashboard/replay.json');
const race = args.includes('--race');
const raceModels = (process.env.RACE_MODELS ?? 'anthropic/claude-haiku-4.5,openai/gpt-5.6').split(',').map((s) => s.trim()).filter(Boolean);

const client = createClient();
if (race && client.provider !== 'gateway') { console.error('--race needs the gateway provider (AI_GATEWAY_API_KEY): the other models are timed through the same gateway'); process.exit(1); }

async function timeWritingModel(model, state, questions) {
  const t0 = performance.now();
  const res = await fetch('https://ai-gateway.vercel.sh/v1/chat/completions', {
    method: 'POST',
    headers: { Authorization: `Bearer ${process.env.AI_GATEWAY_API_KEY}`, 'content-type': 'application/json' },
    body: JSON.stringify({
      model,
      messages: [
        { role: 'system', content: 'Answer the questions about the state. Reply with one JSON object: for each question id, the chosen option (or true/false, or the level index) and a probability from 0 to 1. No prose.' },
        { role: 'user', content: JSON.stringify({ state, questions: encodeQuestions(questions, 'gateway') }) },
      ],
      max_tokens: 200,
    }),
  });
  await res.text();
  return { ms: Math.round(performance.now() - t0), ok: res.ok, status: res.status };
}

async function raceFor(state, questions) {
  if (!race) return null;
  const result = {};
  for (const m of raceModels) {
    try { result[m] = await timeWritingModel(m, state, questions); } catch (err) { result[m] = { ms: null, ok: false, error: err.message }; }
  }
  return result;
}

const events = [];
let t = 0;

// --- the gate: run 1 of the fake day ------------------------------------------------------
const day = JSON.parse(readFileSync('guard/actions.json', 'utf8'));
const guard = createGuard({ client, rules: { budgetUsd: day.budgetUsd } });
const history = [];
let spent = 0;
const gateEvents = [];
for (const action of day.actions) {
  const entry = await guard.check(action, { goal: day.goal, history, spentTodayUsd: spent, run: 1 });
  let status;
  if (entry.outcome === 'run') { spent += Number(action.cost_usd ?? 0); status = action.simulatedStatus ?? 'ok'; }
  else status = entry.outcome === 'hold' ? 'held' : 'denied';
  const stateForRace = buildState(action, { goal: day.goal, history }, guard.rules);
  const raceResult = entry.answers ? await raceFor(stateForRace, buildQuestions({ hasLead: Boolean(action.lead) })) : null;
  history.push({ type: action.type, description: action.description, cost_usd: action.cost_usd, status });
  gateEvents.push({
    kind: 'gate',
    id: action.id, type: action.type, description: action.description, cost_usd: action.cost_usd, expected: action.expected,
    ms: entry.ms, outcome: entry.outcome, reason: entry.reason, class: entry.class, threshold: entry.threshold,
    answers: entry.answers, checks: entry.checks ?? null, usd: entry.usage?.estimatedUsd ?? 0, inputTokens: entry.usage?.inputTokens ?? 0,
    race: raceResult,
  });
  process.stdout.write(`gate ${action.id} ${entry.outcome} ${entry.ms ?? '-'}ms\n`);
}

// --- the business: both quote demos ------------------------------------------------------
const quoteEvents = [];
for (const path of ['quote/configs/desert-air-hvac.json', 'b2b/configs/valley-sign-co.json']) {
  const config = loadConfig(path);
  const engine = createEngine(config, client);
  for (const input of config.demoInputs) {
    const r = await engine.quote(input.text);
    let followUp = null;
    if (r.kind === 'ask' && input.followUp != null) followUp = { value: input.followUp, decision: engine.answer(r.pendingId, input.followUp) };
    const state = config.profile === 'trade'
      ? { business: config.business.name, trade: config.tableData.trade, city: config.business.city, message: input.text }
      : { business: config.business.name, message: input.text };
    const raceResult = await raceFor(state, engine.questions);
    const strip = (d) => { const { answers, pendingId, sources, ...rest } = d; return rest; };
    quoteEvents.push({
      kind: 'quote', business: config.business.name, profile: config.profile, trade: config.tableData?.trade ?? 'signs', city: config.business.city,
      text: input.text, ms: r.ms, answers: r.answers, decision: strip(r), followUp: followUp ? { value: followUp.value, decision: strip(followUp.decision) } : null,
      usd: 0, race: raceResult,
    });
    process.stdout.write(`quote "${input.text.slice(0, 40)}" ${r.kind} ${Math.round(r.ms)}ms\n`);
  }
}

// interleave: roughly three gate checks per business request, like a real afternoon
let qi = 0;
for (let i = 0; i < gateEvents.length; i++) {
  events.push({ ...gateEvents[i], t });
  t += Math.max(700, Math.round(gateEvents[i].ms ?? 0)) + 1200;
  if (i % 3 === 2 && qi < quoteEvents.length) { events.push({ ...quoteEvents[qi++], t }); t += 1800 + Math.round(quoteEvents[qi - 1].ms); }
}
while (qi < quoteEvents.length) { events.push({ ...quoteEvents[qi++], t }); t += 1800; }

const replay = {
  recordedAt: new Date().toISOString(),
  provider: client.provider,
  model: client.model,
  mock: client.provider === 'mock',
  race: race ? raceModels : null,
  goal: day.goal,
  budgetUsd: day.budgetUsd,
  note: client.provider === 'mock'
    ? 'Recorded with the offline mock provider: keyword heuristics standing in for jev. Latencies and answers say nothing about jev. Put AI_GATEWAY_API_KEY in .env and re-run record.js for a real recording.'
    : 'Replay of real recorded calls, at real speed. Nothing was executed or sent.',
  events,
};
writeFileSync(out, JSON.stringify(replay, null, 1));
console.log(`\n${events.length} events → ${out}  (provider ${client.provider}${race ? `, race vs ${raceModels.join(', ')}` : ''})`);
