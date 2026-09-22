// Offline stand-in for jev. Deterministic keyword heuristics that return the same wire shape
// as the Vercel AI Gateway evaluation endpoint, so every script in this folder runs without a
// key. It is NOT jev and its numbers prove nothing about jev; it exists so the code paths,
// thresholds and logs can be exercised and tested offline. Select it with JEV_PROVIDER=mock.

const STOP = new Set('a an the and or of to for in on at is it my our we i me this that with from by be as'.split(' '));

export function tokens(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value ?? '');
  return text
    .toLowerCase()
    .replace(/[^a-z0-9$+]+/g, ' ')
    .split(' ')
    .filter((w) => w && !STOP.has(w))
    .map(stem);
}
export function stem(w) {
  if (w.length > 3 && w.endsWith('ies')) return w.slice(0, -3) + 'y';
  if (w.length > 3 && w.endsWith('s') && !w.endsWith('ss')) return w.slice(0, -1);
  return w;
}
// The text the question is really about. State objects carry context (business name, city,
// history) that must not leak into keyword matching, so prefer the message-like field.
function focusOf(state) {
  if (typeof state === 'string') return state;
  if (state && typeof state === 'object') {
    for (const k of ['message', 'text', 'document', 'input', 'query']) if (typeof state[k] === 'string') return state[k];
    if (state.action) return typeof state.action === 'string' ? state.action : `${state.action.type ?? ''} ${state.action.description ?? ''}`;
  }
  return JSON.stringify(state ?? '');
}

function overlap(a, b) {
  const setB = new Set(b);
  let n = 0;
  for (const w of new Set(a)) if (setB.has(w)) n += 1;
  return n;
}
function jaccard(a, b) {
  const A = new Set(a);
  const B = new Set(b);
  if (A.size === 0 && B.size === 0) return 0;
  let inter = 0;
  for (const w of A) if (B.has(w)) inter += 1;
  return inter / (A.size + B.size - inter);
}
function softmax(scores, temperature = 0.6) {
  const max = Math.max(...scores);
  const exps = scores.map((s) => Math.exp((s - max) / temperature));
  const sum = exps.reduce((a, b) => a + b, 0);
  return exps.map((e) => e / sum);
}
const clamp = (p) => Math.min(0.99, Math.max(0.01, p));
const round = (p) => Math.round(p * 1000) / 1000;
// single words are compared stemmed; phrases are searched in the raw lowercased text
const has = (words, list, text = words.join(' ')) => list.some((k) => (k.includes(' ') ? text.includes(k) : words.includes(stem(k))));
const textOf = (v) => (typeof v === 'string' ? v : JSON.stringify(v ?? ''));

const NOT_REAL = ['seo', 'backlink', 'crypto', 'bitcoin', 'guaranteed', 'http', 'www', 'click', 'unsubscribe', 'casino', 'loan', 'ranking', 'asdf', 'lorem', 'test'];
const VISIT = ['noise', 'weird', 'smell', 'sometime', 'intermittent', 'not sure', 'unsure', 'strange', 'leak', 'everything', 'whole', 'all our', 'multiple', 'locations', 'custom', 'inspect', 'look'];
const IRREVERSIBLE = ['delete', 'drop', 'send', 'mail', 'email', 'pay', 'invoice', 'order', 'purchase', 'wire', 'transfer', 'publish', 'post', 'deploy', 'cancel', 'refund'];
const REVERSIBLE = ['draft', 'render', 'save', 'edit', 'read', 'search', 'log', 'scrape', 'index', 'list', 'fetch', 'summarize', 'write', 'preview', 'build', 'check', 'review', 'note'];
const RISKY = ['unverified', 'unknown', 'database', 'all', 'entire', 'production', 'bulk', 'wire', 'unlisted', 'personal', 'scraped', 'suspicious'];

function answerChoice(q, stateWords, state) {
  const instr = textOf(q.instructions).toLowerCase();
  const names = Object.keys(q.criteria);
  const focus = focusOf(state).toLowerCase();

  // guard-style "may it spend" question: arithmetic on cost plus risk words. approve/review/deny.
  if (/spend|approve/.test(instr) && names.includes('deny')) {
    const action = state?.action ?? {};
    const cost = Number(action.cost_usd ?? action.cost ?? 0);
    const words = tokens(action);
    const risky = has(words, RISKY);
    const irreversible = has(words, IRREVERSIBLE);
    let dist;
    if (cost === 0 && !risky) dist = { approve: 0.94, review: 0.05, deny: 0.01 };
    else if (cost <= 30 && !risky) dist = { approve: 0.9, review: 0.08, deny: 0.02 };
    else if (cost <= 100 && !risky) dist = { approve: irreversible ? 0.62 : 0.78, review: irreversible ? 0.3 : 0.18, deny: 0.04 };
    else if (cost <= 250 && !risky) dist = { approve: 0.35, review: 0.5, deny: 0.15 };
    else dist = { approve: 0.03, review: 0.12, deny: 0.85 };
    if (risky && cost > 0) dist = { approve: 0.02, review: 0.1, deny: 0.88 };
    if (risky && cost === 0) dist = irreversible ? { approve: 0.05, review: 0.2, deny: 0.75 } : { approve: 0.3, review: 0.6, deny: 0.1 };
    const probabilities = Object.fromEntries(names.map((n) => [n, round(dist[n] ?? 0.01)]));
    const choice = names.reduce((a, b) => (probabilities[a] >= probabilities[b] ? a : b));
    return { type: 'choice', choice, probabilities };
  }

  const scores = names.map((name) => {
    const desc = textOf(q.criteria[name] ?? '').toLowerCase();
    const own = tokens(`${name} ${desc}`);
    // phrases in the description ("warm air", "all our locations") count double when present verbatim
    const phrases = desc.split(/[,;]/).map((x) => x.trim()).filter((x) => x.includes(' ') && focus.includes(x)).length;
    return overlap(own, stateWords) + phrases * 2;
  });
  const total = scores.reduce((a, b) => a + b, 0);
  let probs;
  if (total === 0) {
    const other = names.findIndex((n) => /other|unknown|none/.test(n));
    probs = names.map((_, i) => (other >= 0 ? (i === other ? 0.55 : 0.45 / (names.length - 1)) : 1 / names.length));
  } else {
    probs = softmax(scores.map((s) => s * 1.6));
  }
  const probabilities = Object.fromEntries(names.map((n, i) => [n, round(clamp(probs[i]))]));
  const choice = names.reduce((a, b) => (probabilities[a] >= probabilities[b] ? a : b));
  return { type: 'choice', choice, probabilities };
}

function answerScore(q, stateWords, state) {
  const focus = focusOf(state).toLowerCase();
  const levels = q.criteria.map((c) => textOf(c).toLowerCase());
  const scores = levels.map((desc) => {
    const phrases = desc.split(/[,;:]/).map((x) => x.trim()).filter((x) => x.includes(' ') && focus.includes(x)).length;
    return overlap(tokens(desc), stateWords) + phrases * 2;
  });
  const total = scores.reduce((a, b) => a + b, 0);
  let probs;
  if (total === 0) {
    // nothing in the text says where on the scale this is: lean to the low/middle, stay unsure
    const n = levels.length;
    probs = levels.map((_, i) => (i === 0 ? 0.42 : i === 1 ? 0.38 : 0.2 / (n - 2)));
    if (n === 2) probs = [0.55, 0.45];
  } else {
    probs = softmax(scores.map((s) => s * 1.4));
  }
  const probabilities = Object.fromEntries(probs.map((p, i) => [String(i), round(clamp(p))]));
  const score = probs.reduce((acc, p, i) => acc + p * i, 0);
  return { type: 'score', score: round(score), probabilities };
}

function answerBoolean(q, stateWords, state) {
  const instr = textOf(q.instructions).toLowerCase();
  const action = state?.action;
  const actionWords = action ? tokens(action.description ?? action) : stateWords;
  const focus = focusOf(state).toLowerCase();
  let p;
  if (/duplicate|already done|repeat/.test(instr)) {
    const history = state?.last_actions ?? state?.history ?? [];
    let best = 0;
    for (const h of history) {
      // a different kind of action on the same target is not a repeat: weight by type
      const sameType = !action?.type || !h.type || String(h.type).toLowerCase() === String(action.type).toLowerCase();
      best = Math.max(best, jaccard(actionWords, tokens(h.description ?? h)) * (sameType ? 1 : 0.3));
    }
    p = best >= 0.9 ? 0.95 : best >= 0.75 ? 0.5 + (best - 0.75) * 1.8 : best * 0.25;
  } else if (/off goal|off-goal|goal/.test(instr)) {
    const goalWords = tokens(state?.goal ?? '');
    const n = overlap(actionWords, goalWords);
    p = n >= 2 ? 0.06 : n === 1 ? 0.25 : 0.9;
  } else if (/undone|revers|undo/.test(instr)) {
    if (has(actionWords, IRREVERSIBLE)) p = 0.08;
    else if (has(actionWords, REVERSIBLE)) p = 0.92;
    else p = 0.5;
  } else if (/last step|previous step|last action|most recent action|executed|actually work/.test(instr)) {
    const history = state?.last_actions ?? state?.history ?? [];
    const last = history[history.length - 1];
    const status = String(last?.status ?? last?.result ?? '').toLowerCase();
    p = !last ? 0.5 : /ok|success|done|sent|200/.test(status) ? 0.93 : /fail|error|timeout|bounce|5\d\d/.test(status) ? 0.07 : 0.5;
  } else if (/real|genuine|spam|legit/.test(instr)) {
    const threadText = Array.isArray(state?.thread) ? state.thread.map((m) => (typeof m === 'string' ? m : m.text ?? '')).join(' ') : null;
    const words = threadText != null ? tokens(threadText) : stateWords;
    if (has(words, NOT_REAL, threadText ?? focus) || words.length < 2) p = 0.05;
    else p = threadText != null && state.thread.length > 1 ? 0.94 : 0.9;
  } else if (/visit|inspect|site|in person|look at/.test(instr)) {
    p = has(stateWords, VISIT, focus) ? 0.89 : 0.12;
  } else if (/rush|urgent|hurry|deadline/.test(instr)) {
    p = has(stateWords, ['rush', 'urgent', 'asap', 'tomorrow', 'today', 'deadline', 'friday', 'next week', 'this week', 'by the end'], focus) && !focus.includes('no rush') ? 0.88 : 0.1;
  } else if (/install/.test(instr)) {
    p = has(stateWords, ['install', 'installed', 'mount', 'mounted', 'hang', 'hung', 'put up'], focus) ? 0.9 : 0.12;
  } else if (/design|artwork|logo/.test(instr)) {
    p = has(stateWords, ['design', 'logo', 'mockup', 'artwork', 'no artwork', 'branding'], focus) ? 0.88 : 0.15;
  } else if (/call|conversation|phone/.test(instr)) {
    p = has(stateWords, ['locations', 'all our', 'multiple', 'custom', 'everything', 'project', 'budget', 'not sure', 'complicated', 'rebrand', 'signage'], focus) ? 0.91 : 0.1;
  } else {
    const t = tokens(textOf(q.criteria?.true ?? ''));
    const f = tokens(textOf(q.criteria?.false ?? ''));
    const a = overlap(t, stateWords);
    const b = overlap(f, stateWords);
    p = a === b ? 0.5 : a > b ? 0.8 : 0.2;
  }
  return { type: q.type === 'noul' ? 'noul' : 'boolean', ...(q.type === 'noul' ? { noul: round(clamp(p)) } : { probability: round(clamp(p)) }) };
}

export function mockDecide(state, wireQuestions) {
  const stateWords = tokens(focusOf(state));
  const answers = {};
  for (const [id, q] of Object.entries(wireQuestions)) {
    if (q.type === 'choice') answers[id] = answerChoice(q, stateWords, state);
    else if (q.type === 'score') answers[id] = answerScore(q, stateWords, state);
    else answers[id] = answerBoolean(q, stateWords, state);
  }
  const inputTokens = Math.ceil((JSON.stringify(state).length + JSON.stringify(wireQuestions).length) / 4);
  return { model: 'mock', answers, usage: { inputTokens, outputTokens: 0 }, warnings: [{ type: 'other', message: 'mock provider: keyword heuristics, not jev' }] };
}
