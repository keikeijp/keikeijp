import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { loadConfig, createEngine, parseQuantity, parseSqft, bandBySqft, bandByQuantity, lookupTradeRow } from '../quote/engine.js';
import { validate } from '../quote/validate-table.js';
import { createClient } from '../lib/jev.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const trade = loadConfig(join(root, 'quote/configs/desert-air-hvac.json'));
const b2b = loadConfig(join(root, 'b2b/configs/valley-sign-co.json'));

// fixed answers, so these test the decision rules and not the model
const C = (choice, p) => ({ type: 'choice', choice, probability: p, probabilities: { [choice]: p } });
const S = (level, p) => ({ type: 'scale', level, probability: p });
const B = (p) => ({ type: 'bool', probability: p, value: p >= 0.5 });
const tradeAnswers = (over = {}) => ({ service: C('AC Repair', 0.95), size: S('medium', 0.8), urgency: S('today', 0.9), needs_visit: B(0.1), is_real: B(0.95), ...over });
const b2bAnswers = (over = {}) => ({ item: C('yard_signs', 0.95), quantity: S('bulk', 0.8), rush: B(0.1), installation: B(0.1), design: B(0.1), needs_call: B(0.1), ...over });

test('parseQuantity ignores dimensions, prices and sizes', () => {
  assert.equal(parseQuantity('40 yard signs for the election'), 40);
  assert.equal(parseQuantity('qty: 1,000 flyers'), 1000);
  assert.equal(parseQuantity('a channel letter sign, about 18 inch letters'), null);
  assert.equal(parseQuantity('a 12 ft banner'), null);
  assert.equal(parseQuantity('4x8 banner, 2 of them'), 2);
  assert.equal(parseQuantity('need 6 window decals for a 2,000 sq ft store'), 6);
  assert.equal(parseQuantity('$500 budget for signs'), null);
});

test('parseSqft and the size bands', () => {
  assert.equal(parseSqft('our 3,200 sq ft house'), 3200);
  assert.equal(parseSqft('1800 square feet'), 1800);
  assert.equal(parseSqft('4 ton unit'), null);
  assert.equal(bandBySqft(trade.sizeQuestion.bands, 900), 'small');
  assert.equal(bandBySqft(trade.sizeQuestion.bands, 1200), 'small');
  assert.equal(bandBySqft(trade.sizeQuestion.bands, 1800), 'medium');
  assert.equal(bandBySqft(trade.sizeQuestion.bands, 9000), 'large');
});

test('quantity bands are contiguous and the top band is a call', () => {
  const bands = b2b.catalogData.quantityBands;
  assert.equal(bandByQuantity(bands, 1), 'single');
  assert.equal(bandByQuantity(bands, 5), 'few');
  assert.equal(bandByQuantity(bands, 6), 'batch');
  assert.equal(bandByQuantity(bands, 100), 'bulk');
  assert.equal(bandByQuantity(bands, 101), null);
});

test('trade decisions: the table picks the price, jev picks the row', () => {
  const client = createClient({ env: { JEV_PROVIDER: 'mock' } });
  const engine = createEngine(trade, client);
  const row = lookupTradeRow(trade.tableData, 'repair', 'medium', 'today');
  assert.deepEqual(engine.decide(tradeAnswers(), {}), { kind: 'range', low: row.low, high: row.high, service: 'AC Repair', row: 'repair', size: 'medium', urgency: 'today', sources: [] });
  assert.equal(engine.decide(tradeAnswers({ is_real: B(0.2) }), {}).kind, 'ignore');
  assert.equal(engine.decide(tradeAnswers({ service: C('AC Repair', 0.6) }), {}).kind, 'visit');
  assert.equal(engine.decide(tradeAnswers({ needs_visit: B(0.89) }), {}).kind, 'visit');
  assert.equal(engine.decide(tradeAnswers({ service: C('Something else', 0.9) }), {}).kind, 'visit');
  const ask = engine.decide(tradeAnswers({ size: S('small', 0.63) }), {});
  assert.equal(ask.kind, 'ask');
  assert.equal(ask.field, 'sqft');
  // a number answers the size question with arithmetic
  assert.equal(engine.decide(tradeAnswers({ size: S('small', 0.63) }), { sqft: 2600 }).size, 'large');
  assert.equal(engine.decide(tradeAnswers({ size: S('small', 0.63) }), { text: 'tune up for our 3200 sq ft house' }).size, 'large');
});

test('trade follow-up flow keeps the first answers and makes no second call', async () => {
  let calls = 0;
  const client = { provider: 'fake', decide: async () => { calls += 1; return { answers: tradeAnswers({ size: S('small', 0.6) }), ms: 5 }; } };
  const owner = [];
  const engine = createEngine(trade, client, { notify: async (e) => owner.push(e) });
  const first = await engine.quote('my ac is blowing warm air');
  assert.equal(first.kind, 'ask');
  const second = engine.answer(first.pendingId, '1800');
  assert.equal(second.kind, 'range');
  assert.equal(second.size, 'medium');
  assert.equal(calls, 1);
  assert.equal(owner.length, 1); // owner texted once, with the range
  assert.equal(engine.answer(first.pendingId, '1800').kind, 'expired');
  assert.equal(engine.answer('nope', '1').kind, 'expired');
});

test('spam never reaches the owner', async () => {
  const client = { provider: 'fake', decide: async () => ({ answers: tradeAnswers({ is_real: B(0.05) }), ms: 1 }) };
  const owner = [];
  const engine = createEngine(trade, client, { notify: async (e) => owner.push(e) });
  const r = await engine.quote('buy backlinks');
  assert.equal(r.kind, 'ignore');
  assert.equal(owner.length, 0);
});

test('b2b decisions: call on needs_call, low item confidence, over the top band, missing band', () => {
  const engine = createEngine(b2b, createClient({ env: { JEV_PROVIDER: 'mock' } }));
  const r = engine.decide(b2bAnswers(), { text: '40 yard signs' });
  assert.equal(r.kind, 'range');
  assert.equal(r.band, 'bulk');
  assert.equal(r.quantity, 40);
  assert.deepEqual([r.low, r.high], b2b.catalogData.items.yard_signs.bands.bulk);
  assert.equal(engine.decide(b2bAnswers({ needs_call: B(0.91) }), { text: 'signage for all our locations' }).kind, 'call');
  assert.equal(engine.decide(b2bAnswers({ item: C('yard_signs', 0.5) }), { text: 'signs' }).kind, 'call');
  assert.equal(engine.decide(b2bAnswers(), { text: '250 yard signs' }).kind, 'call');
  assert.equal(engine.decide(b2bAnswers({ item: C('monument_sign', 0.9), quantity: S('few', 0.9) }), { text: 'monument signs' }).kind, 'call');
  assert.equal(engine.decide(b2bAnswers({ quantity: S('few', 0.5) }), { text: 'yard signs please' }).kind, 'ask');
  const flagged = engine.decide(b2bAnswers({ rush: B(0.9), design: B(0.88) }), { text: '10 yard signs' });
  assert.equal(flagged.note, 'rush, design work');
});

test('validator catches overlapping bands and missing sources', () => {
  const ok = validate(trade.tableData);
  assert.equal(ok.ok, true);
  assert.equal(ok.drops.length, Object.keys(trade.tableData.services).length); // sample table: no sources yet
  const bad = structuredClone(trade.tableData);
  bad.sizes[1].maxSqft = 1000; // overlaps small (1200)
  bad.services.repair.rows.small.today = [900, 100];
  const r = validate(bad);
  assert.equal(r.ok, false);
  assert.ok(r.problems.some((p) => /overlaps/.test(p)));
  assert.ok(r.problems.some((p) => /bad range/.test(p)));
  const cat = structuredClone(b2b.catalogData);
  cat.quantityBands[1].min = 3; // gap after "single"
  assert.ok(validate(cat).problems.some((p) => /contiguous/.test(p)));
});
