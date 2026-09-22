#!/usr/bin/env node
// Guard one action. The agent pipes JSON in, gets JSON out, and the exit code says what to do:
//   0 run   2 hold (ask a person)   3 deny   1 error
//
//   echo '{"goal":"...","action":{"type":"email","description":"...","cost_usd":0},"history":[...],"spentTodayUsd":12}' \
//     | node guard/cli.js [--budget 100] [--log logs/guard.jsonl]
import { loadEnv } from '../lib/env.js';
import { createClient } from '../lib/jev.js';
import { createGuard } from './guard.js';

loadEnv();
const args = process.argv.slice(2);
const flag = (name, def) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : def; };

let input = '';
for await (const chunk of process.stdin) input += chunk;
let req;
try { req = JSON.parse(input); } catch (e) { console.error(`invalid JSON on stdin: ${e.message}`); process.exit(1); }
if (!req.action || !req.goal) { console.error('need {"goal": "...", "action": {"type","description","cost_usd"}, "history": [...]}'); process.exit(1); }

const guard = createGuard({
  client: createClient(),
  rules: { budgetUsd: Number(flag('budget', req.budgetUsd ?? 100)), lite: args.includes('--lite') },
  logFile: args.includes('--lite') ? null : flag('log', 'logs/guard.jsonl'),
});
const entry = await guard.check(req.action, { goal: req.goal, history: req.history ?? [], spentTodayUsd: req.spentTodayUsd ?? 0 });
console.log(JSON.stringify(entry, null, 2));
process.exit(entry.outcome === 'run' ? 0 : entry.outcome === 'hold' ? 2 : 3);
