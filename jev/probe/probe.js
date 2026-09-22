#!/usr/bin/env node
// Step 0 from the guide: "set up jev for me and prove it works".
// Sends one situation + a list of options, prints the answer, the probability and the round-trip time.
// A failure prints the actual error (status, body, url) and exits 1. Nothing is retried.
//
//   node probe/probe.js
//   node probe/probe.js "my ac is blowing warm air and it's 110 out" repair install maintenance other
//   JEV_PROVIDER=mock node probe/probe.js      # offline stand-in, proves the code path only

import { loadEnv } from '../lib/env.js';
import { createClient, choice, JevError } from '../lib/jev.js';

loadEnv();
const [situationArg, ...optionArgs] = process.argv.slice(2);
const situation = situationArg ?? 'my ac is blowing warm air';
// Describing each option is good practice with jev (it only sees what you show it); bare names work too.
const options = optionArgs.length
  ? optionArgs
  : {
      repair: 'something that used to work is broken: not cooling, blowing warm air, making noise, leaking, tripping the breaker',
      install: 'a new unit, a replacement system, a new build or an addition',
      maintenance: 'a tune-up, seasonal check-up, filter change or inspection with nothing broken',
      other: 'anything that is not one of the above',
    };

let client;
try {
  client = createClient();
} catch (err) {
  console.error(`✗ ${err.message}`);
  process.exit(1);
}

console.log(`provider: ${client.provider}   model: ${client.model}   url: ${client.url ?? '-'}`);
console.log(`situation: ${situation}`);
console.log(`options:   ${Array.isArray(options) ? options.join(', ') : Object.keys(options).join(', ')}`);

try {
  const { answers, ms, usage } = await client.decide(situation, {
    service: choice('Which option best describes what this situation needs?', options),
  });
  const a = answers.service;
  console.log('');
  console.log(`answer:      ${a.choice}`);
  console.log(`probability: ${a.probability?.toFixed(3)}`);
  console.log(`distribution: ${JSON.stringify(a.probabilities)}`);
  console.log(`took:        ${ms.toFixed(0)} ms`);
  if (usage?.inputTokens != null) console.log(`input tokens: ${usage.inputTokens}  (≈ $${usage.estimatedUsd.toFixed(6)})`);
  if (client.provider === 'mock') console.log('\n(mock provider: this proves the script runs, not that jev answers. Put a key in .env to call jev.)');
} catch (err) {
  console.error('\n✗ jev call failed');
  if (err instanceof JevError) {
    if (err.status) console.error(`  status: ${err.status}`);
    if (err.url) console.error(`  url:    ${err.url}`);
    console.error(`  error:  ${err.message}`);
    if (err.body && typeof err.body === 'object') console.error(`  body:   ${JSON.stringify(err.body, null, 2)}`);
    if (err.cause) console.error(`  cause:  ${err.cause?.message ?? err.cause}`);
  } else {
    console.error(err);
  }
  process.exit(1);
}
