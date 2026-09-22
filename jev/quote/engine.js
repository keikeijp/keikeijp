// Quote engine shared by the local-trades widget (profile "trade") and the b2b quote desk
// (profile "b2b"). One jev call classifies the request; the code looks the answer up in a
// price table built from public pricing. The table picks the price, jev only picks the row.
//
// Every decision is one of:
//   { kind: 'range',  low, high, ... }   show a price range
//   { kind: 'visit' }  / { kind: 'call' } show "we'll confirm within the hour" + booking button
//   { kind: 'ask', question, pendingId }  one follow-up question, answered without a second jev call
//   { kind: 'ignore' }                    not a real request: show nothing to the owner

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { choice, scale, bool } from '../lib/jev.js';

export const DEFAULT_THRESHOLDS = {
  service: 0.7, // below this on "which service": don't show a number
  size: 0.7, // below this on size: ask one follow-up question
  needsVisit: 0.5, // P(true) at or above this: book a visit
  isReal: 0.5, // P(true) below this: spam, show nothing to the owner
  item: 0.7,
  quantity: 0.7,
  needsCall: 0.5,
};

export function loadConfig(path) {
  const abs = resolve(path);
  const config = JSON.parse(readFileSync(abs, 'utf8'));
  const dir = dirname(abs);
  if (config.table) config.tableData = JSON.parse(readFileSync(resolve(dir, config.table), 'utf8'));
  if (config.catalog) config.catalogData = JSON.parse(readFileSync(resolve(dir, config.catalog), 'utf8'));
  config.thresholds = { ...DEFAULT_THRESHOLDS, ...(config.thresholds ?? {}) };
  config.configDir = dir;
  return config;
}

// ---- profile: trade -------------------------------------------------------------------------

function tradeQuestions(config) {
  const table = config.tableData;
  const services = Object.fromEntries(config.services.map((s) => [s.name, s.description ?? null]));
  return {
    service: choice('Which of this business\'s services is the customer asking for?', services),
    size: scale('How big is this job?', table.sizes.map((s) => [s.name, s.description ?? null])),
    urgency: scale('How urgent is it?', table.urgencies.map((u) => [u.name, u.description ?? null])),
    needs_visit: bool('Does someone need to visit the property before a price can be given?', {
      true: 'the problem is vague, intermittent, unusual, or the scope cannot be judged from the text',
      false: 'the job is a standard, clearly described service that can be priced from a table',
    }),
    is_real: bool('Is this a genuine service request from a real customer?', {
      true: 'a person describing a real problem or job at a property',
      false: 'spam, marketing, a test message, gibberish, or nothing to do with this trade',
    }),
  };
}

export function lookupTradeRow(table, rowKey, size, urgency) {
  const service = table.services[rowKey];
  const row = service?.rows?.[size]?.[urgency];
  if (!row) return null;
  return { low: row[0], high: row[1], sources: service.sources ?? [] };
}

// "3,200 sq ft" in the message is arithmetic, not a question for the model
export function parseSqft(text) {
  const m = String(text ?? '').match(/(\d[\d,]*)\s*(?:sq\.?\s*ft\.?|sqft|square\s*feet|square\s*foot|sf)\b/i);
  if (!m) return null;
  const n = Number(m[1].replace(/,/g, ''));
  return Number.isFinite(n) && n > 0 ? n : null;
}

export function bandBySqft(bands, sqft) {
  for (const b of bands) if (b.maxSqft == null || sqft <= b.maxSqft) return b.level;
  return bands[bands.length - 1].level;
}

function decideTrade(config, answers, extra = {}) {
  const t = config.thresholds;
  const a = answers;
  if (a.is_real.probability < t.isReal) return { kind: 'ignore', reason: `is_real ${a.is_real.probability}` };

  const svc = config.services.find((s) => s.name === a.service.choice);
  if (a.service.probability < t.service) return { kind: 'visit', reason: `service confidence ${a.service.probability} < ${t.service}` };
  if (!svc || !svc.row) return { kind: 'visit', reason: `service "${a.service.choice}" has no price row` };
  if (a.needs_visit.probability >= t.needsVisit) return { kind: 'visit', reason: `needs_visit ${a.needs_visit.probability}` };

  let size = a.size.level;
  const sqft = extra.sqft ?? parseSqft(extra.text);
  if (sqft != null) size = bandBySqft(config.sizeQuestion.bands, Number(sqft));
  else if (a.size.probability < t.size) {
    return { kind: 'ask', field: 'sqft', question: config.sizeQuestion.prompt, reason: `size confidence ${a.size.probability} < ${t.size}` };
  }
  const urgency = a.urgency.level;
  const row = lookupTradeRow(config.tableData, svc.row, size, urgency);
  if (!row) return { kind: 'visit', reason: `no table row for ${svc.row}/${size}/${urgency}` };
  return { kind: 'range', low: row.low, high: row.high, service: svc.name, row: svc.row, size, urgency, sources: row.sources };
}

// ---- profile: b2b ---------------------------------------------------------------------------

function b2bQuestions(config) {
  const cat = config.catalogData;
  const items = Object.fromEntries(Object.entries(cat.items).map(([k, v]) => [k, v.description ?? v.label ?? null]));
  return {
    item: choice('Which catalog item is being asked for?', items),
    quantity: scale('Roughly how many?', cat.quantityBands.map((b) => [b.name, b.description ?? null])),
    rush: bool('Is this a rush job?', { true: 'needs it faster than the normal turnaround', false: 'normal turnaround is fine or no deadline given' }),
    installation: bool('Does it need installation by the shop?', { true: 'asks for installing, mounting or hanging', false: 'pickup, shipping, or nothing said about installation' }),
    design: bool('Does it need design work?', { true: 'no artwork yet, asks for a logo, mockup or design', false: 'print-ready artwork exists or nothing said' }),
    needs_call: bool('Does this need a conversation before it can be quoted?', {
      true: 'a multi-location or custom project, a rebrand, an unclear scope, or a budget question',
      false: 'one clearly described item and quantity',
    }),
  };
}

// A number in the message is the quantity only when it isn't a dimension, a price, a size or a time.
const NOT_QTY_AFTER = /^\s*(?:"|'|in\b|inch|inches|ft\b|feet|foot|mm\b|cm\b|m\b|sq|square|x\s*\d|%|k\b|pm\b|am\b|th\b|st\b|nd\b|rd\b|days?\b|weeks?\b|hours?\b|months?\b|years?\b|gauge|oz\b|lb|point|pt\b|mil\b|bedroom|bed\b|ton\b)/i;
const NOT_QTY_BEFORE = /(?:\$|#|size|by|x|\d\s*x|about|around|roughly|approx\w*|sept?\w*|oct\w*|nov\w*|dec\w*|jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*)\s*$/i;
const QTY_AFTER = /^\s*(?:x\b|pcs|pieces|units|signs?|banners?|shirts?|flyers?|cards?|copies|decals?|stickers?|wraps?|vans?|trucks?|cars?|vehicles?|letters?|windows?|locations?|of\b|sets?|boxes|rolls?|posters?|magnets?|graphics?)/i;
const QTY_BEFORE = /(?:qty|quantity|need|want|order|x|\bfor)\s*:?\s*$/i;
export function parseQuantity(text) {
  const s = String(text ?? '');
  const candidates = [];
  const re = /\d[\d,]*/g;
  let m;
  while ((m = re.exec(s))) {
    const before = s.slice(0, m.index);
    const after = s.slice(m.index + m[0].length);
    if (NOT_QTY_AFTER.test(after) || NOT_QTY_BEFORE.test(before)) continue;
    const n = Number(m[0].replace(/,/g, ''));
    if (!Number.isFinite(n) || n <= 0 || n > 100_000) continue;
    const strong = QTY_AFTER.test(after) || QTY_BEFORE.test(before);
    candidates.push({ n, strong });
  }
  const strong = candidates.filter((c) => c.strong);
  if (strong.length === 1) return strong[0].n;
  if (strong.length === 0 && candidates.length === 1) return candidates[0].n;
  return null; // ambiguous: ask, don't guess
}

export function bandByQuantity(bands, qty) {
  for (const b of bands) if (b.max == null || qty <= b.max) return b.name;
  return null; // above the top band
}

function decideB2b(config, answers, extra = {}) {
  const t = config.thresholds;
  const cat = config.catalogData;
  const a = answers;
  if (a.needs_call.probability >= t.needsCall) return { kind: 'call', reason: `needs_call ${a.needs_call.probability}` };
  if (a.item.probability < t.item) return { kind: 'call', reason: `item confidence ${a.item.probability} < ${t.item}` };
  const item = cat.items[a.item.choice];
  if (!item) return { kind: 'call', reason: `item "${a.item.choice}" not in catalog` };

  // a number in the text beats a model guess: arithmetic where arithmetic works
  let band;
  const qty = extra.quantity ?? parseQuantity(extra.text ?? '');
  if (qty != null) {
    band = bandByQuantity(cat.quantityBands, qty);
    if (band === null) return { kind: 'call', reason: `quantity ${qty} is over the top band`, quantity: qty };
  } else if (a.quantity.probability < t.quantity) {
    return { kind: 'ask', field: 'quantity', question: config.quantityQuestion?.prompt ?? 'Roughly how many do you need?', reason: `quantity confidence ${a.quantity.probability} < ${t.quantity}` };
  } else {
    band = a.quantity.level;
  }
  const row = item.bands?.[band];
  if (!row) return { kind: 'call', reason: `no band "${band}" for ${a.item.choice}` };
  const flags = { rush: a.rush.value, installation: a.installation.value, design: a.design.value };
  return {
    kind: 'range', low: row[0], high: row[1], item: a.item.choice, label: item.label, band, quantity: qty ?? null, flags,
    note: [flags.rush && 'rush', flags.installation && 'installation', flags.design && 'design work'].filter(Boolean).join(', ') || null,
    sources: item.sources ?? [],
  };
}

// ---- engine -------------------------------------------------------------------------------

export function createEngine(config, client, { notify, timeoutMs = 3000 } = {}) {
  const profile = config.profile;
  if (!['trade', 'b2b'].includes(profile)) throw new Error(`unknown profile "${profile}"`);
  const questions = profile === 'trade' ? tradeQuestions(config) : b2bQuestions(config);
  const decideFn = profile === 'trade' ? decideTrade : decideB2b;
  const pending = new Map(); // pendingId -> { answers, text, expires }

  function finish(decision, text, answers, ms) {
    const result = { ...decision, answers: summarize(answers), ms };
    if (decision.kind === 'ask') {
      const pendingId = randomUUID();
      pending.set(pendingId, { answers, text, expires: Date.now() + 15 * 60_000 });
      result.pendingId = pendingId;
    }
    if (notify && ['range', 'visit', 'call'].includes(decision.kind)) {
      notify({ business: config.business, text, decision: result }).catch?.(() => {});
    }
    return result;
  }

  return {
    questions,
    async quote(text) {
      const state = profile === 'trade'
        ? { business: config.business.name, trade: config.tableData.trade, city: config.business.city, message: text }
        : { business: config.business.name, message: text };
      const { answers, ms } = await client.decide(state, questions, { timeoutMs });
      return finish(decideFn(config, answers, { text }), text, answers, ms);
    },
    // one follow-up answer, applied with plain arithmetic: no second jev call
    answer(pendingId, value) {
      const p = pending.get(pendingId);
      if (!p || p.expires < Date.now()) return { kind: 'expired' };
      pending.delete(pendingId);
      const n = Number(String(value).replace(/[^\d.]/g, ''));
      if (!Number.isFinite(n) || n <= 0) return { kind: 'visit', reason: 'follow-up answer was not a number', answers: summarize(p.answers), ms: 0 };
      const extra = profile === 'trade' ? { sqft: n, text: p.text } : { quantity: n, text: p.text };
      return finish(decideFn(config, p.answers, extra), p.text, p.answers, 0);
    },
    decide: (answers, extra) => decideFn(config, answers, extra),
  };
}

export function summarize(answers) {
  const out = {};
  for (const [k, a] of Object.entries(answers)) {
    if (a.type === 'choice') out[k] = { choice: a.choice, p: a.probability };
    else if (a.type === 'scale') out[k] = { level: a.level, p: a.probability };
    else out[k] = { value: a.value, p: a.probability };
  }
  return out;
}
