#!/usr/bin/env node
// Runs the config's demoInputs through the engine and prints what the widget would show,
// plus median latency, call count and estimated cost. Use this before deploying anything.
//   node quote/demo.js --config quote/configs/desert-air-hvac.json
//   JEV_PROVIDER=mock node quote/demo.js --config b2b/configs/valley-sign-co.json
import { loadEnv } from '../lib/env.js';
import { createClient } from '../lib/jev.js';
import { createEngine, loadConfig } from './engine.js';

loadEnv();
const args = process.argv.slice(2);
const i = args.indexOf('--config');
if (i < 0) { console.error('usage: node quote/demo.js --config <config.json>'); process.exit(1); }
const config = loadConfig(args[i + 1]);
const client = createClient();
const owner = [];
const engine = createEngine(config, client, { notify: async (e) => { owner.push(e); } });
const status = (config.tableData ?? config.catalogData)?.status;

console.log(`${config.business.name} (${config.profile})  provider=${client.provider}  table status=${status}`);
if (client.provider === 'mock') console.log('mock provider: outcomes below exercise the code paths, they say nothing about jev\n');
if (status !== 'live') console.log('table/catalog is a SAMPLE: ranges below are placeholders, not market prices\n');

const times = [];
let calls = 0;
let usd = 0;
const show = (r) => r.kind === 'range' ? `$${r.low}–$${r.high}  (${r.service ?? r.label}${r.size ? `, ${r.size}, ${r.urgency}` : `, ${r.band}${r.note ? ', ' + r.note : ''}`})`
  : r.kind === 'ask' ? `ASK: ${r.question}` : r.kind === 'ignore' ? 'nothing shown to owner (not a real request)' : `${r.kind.toUpperCase()}: "we'll confirm within the hour" + book button`;

for (const input of config.demoInputs) {
  const t0 = performance.now();
  let r = await engine.quote(input.text);
  calls += 1; times.push(r.ms);
  console.log(`> ${input.text}`);
  console.log(`  ${show(r)}   [${Math.round(r.ms)} ms]  ${r.reason ? '· ' + r.reason : ''}`);
  console.log(`  answers: ${JSON.stringify(r.answers)}`);
  if (r.kind === 'ask' && input.followUp != null) {
    r = engine.answer(r.pendingId, input.followUp);
    console.log(`  follow-up "${input.followUp}" → ${show(r)}  (no second jev call)`);
  }
  console.log('');
}
const sorted = [...times].sort((a, b) => a - b);
const median = sorted[Math.floor(sorted.length / 2)];
console.log(`calls: ${calls}   median: ${Math.round(median)} ms   owner texts: ${owner.length}`);
