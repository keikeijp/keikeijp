// The guard: before an agent does anything, describe the action to jev and gate it.
//
//   run   - jev is sure enough on every check for this class of action
//   hold  - not sure enough, jev timed out, or the daily budget would be exceeded: a person decides
//   deny  - jev's spend answer is "deny": always stops it, whatever the other numbers say
//
// Thresholds by class (from the guide):
//   normal actions                       0.8
//   cheap actions that can be undone     0.7
//   mailing, ordering, paying, deleting  0.9
// The budget is arithmetic, never a question to the model.

import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { choice, bool, JevTimeoutError } from '../lib/jev.js';

export const DEFAULT_RULES = {
  thresholds: { normal: 0.8, cheapReversible: 0.7, sensitive: 0.9 },
  cheapCostUsd: 5, // at or under this, an action counts as cheap (still needs jev to say it can be undone)
  sensitiveTypes: ['mail', 'email', 'send', 'message', 'text', 'order', 'purchase', 'buy', 'pay', 'invoice', 'transfer', 'wire', 'delete', 'drop', 'destroy'],
  timeoutMs: 3000, // no answer in 3 seconds -> hold
  budgetUsd: 100, // daily, counted in code
  historyLength: 10,
  lite: false, // free version: only the duplicate and off-goal checks, no budget, no spend/deny
};

export function classifyAction(action, rules = DEFAULT_RULES) {
  const type = String(action.type ?? '').toLowerCase();
  const verb = String(action.description ?? '').toLowerCase().split(/\s+/)[0] ?? '';
  const cost = Number(action.cost_usd ?? 0);
  if (rules.sensitiveTypes.some((t) => type === t || type.startsWith(`${t}_`) || type.endsWith(`_${t}`) || verb === t)) return 'sensitive';
  if (cost <= rules.cheapCostUsd) return 'cheap';
  return 'normal';
}

export function buildState(action, ctx, rules = DEFAULT_RULES) {
  const history = (ctx.history ?? []).slice(-rules.historyLength).map((h) => ({
    type: h.type, description: h.description, cost_usd: h.cost_usd ?? 0, status: h.status ?? 'unknown',
  }));
  const state = {
    goal: ctx.goal,
    action: { type: action.type, description: action.description, cost_usd: Number(action.cost_usd ?? 0), touches: action.touches ?? [] },
    last_actions: history,
  };
  if (action.lead) state.thread = action.lead.thread ?? []; // always the full message thread
  return state;
}

export function buildQuestions({ hasLead, lite = false }) {
  const q = {
    duplicate: bool('Is this action a duplicate of something in the last actions that was already done?', {
      true: 'the same thing, or nearly the same thing, was already done and this repeats it',
      false: 'a new step, or a different target, or a legitimate next step after the last one',
    }),
    off_goal: bool("Is this action off goal, i.e. not in service of today's goal?", {
      true: 'unrelated to the goal, a distraction, or something nobody asked for',
      false: 'a reasonable step towards the goal',
    }),
    spend: choice('May this action spend what it costs and touch what it touches?', {
      approve: 'reasonable cost and target for the goal, nothing suspicious',
      review: 'a person should look at it first: unusual cost, unfamiliar target, unclear value',
      deny: 'must not happen: unverified recipients, unknown senders, destroying data, wildly out of proportion',
    }),
    reversible: bool('Can this action be undone afterwards?', {
      true: 'a draft, a render, a file edit, a search, a note: it can be redone or discarded',
      false: 'once done it is out in the world: money moves, a message is sent, data is deleted',
    }),
    last_step_worked: bool('Did the last action in the list actually work?', {
      true: 'its status says it succeeded',
      false: 'it failed, bounced, errored or timed out, and this may be a blind retry',
    }),
  };
  if (lite) return { duplicate: q.duplicate, off_goal: q.off_goal };
  if (hasLead) {
    q.lead_real = bool('Is the lead in this thread a real prospective customer?', {
      true: 'a person with a real, specific need who is engaging',
      false: 'spam, a bot, a vendor pitch, a one-word message, or nothing to go on',
    });
  }
  return q;
}

export function judge(answers, cls, rules = DEFAULT_RULES, { hasLead = false, hasHistory = true } = {}) {
  const t = rules.thresholds;
  const checks = {};
  const failed = [];

  if (rules.lite) {
    const need = (name, p) => { checks[name] = { p, ok: p >= t.normal }; if (p < t.normal) failed.push(`${name} ${p}`); };
    need('not_duplicate', 1 - answers.duplicate.probability);
    need('on_goal', 1 - answers.off_goal.probability);
    return failed.length === 0
      ? { outcome: 'run', reason: `all checks ≥ ${t.normal}`, threshold: t.normal, class: 'lite', checks }
      : { outcome: 'hold', reason: `below ${t.normal}: ${failed.join(', ')}`, threshold: t.normal, class: 'lite', checks };
  }

  // 1. a deny always stops it, whatever the numbers say
  if (answers.spend.choice === 'deny') {
    return { outcome: 'deny', reason: `jev says deny (${answers.spend.probability})`, threshold: null, class: cls, checks: { spend: false } };
  }

  // 2. pick the bar
  let threshold = t.normal;
  let effectiveClass = cls;
  if (cls === 'sensitive') threshold = t.sensitive;
  else if (cls === 'cheap' && answers.reversible.probability >= t.cheapReversible) threshold = t.cheapReversible;
  else if (cls === 'cheap') effectiveClass = 'normal (cheap but not confirmed reversible)';
  if (cls === 'normal' && answers.reversible.probability <= 1 - t.sensitive) {
    // jev is 90%+ sure it cannot be undone: treat like mailing/paying/deleting
    threshold = t.sensitive;
    effectiveClass = 'sensitive (irreversible)';
  }

  // 3. every check must clear the bar
  const need = (name, p, ok) => { checks[name] = { p, ok }; if (!ok) failed.push(`${name} ${p}`); };
  need('not_duplicate', 1 - answers.duplicate.probability, 1 - answers.duplicate.probability >= threshold);
  need('on_goal', 1 - answers.off_goal.probability, 1 - answers.off_goal.probability >= threshold);
  need('spend_approve', answers.spend.probabilities?.approve ?? 0, answers.spend.choice === 'approve' && (answers.spend.probabilities?.approve ?? 0) >= threshold);
  if (hasHistory) need('last_step_worked', answers.last_step_worked.probability, answers.last_step_worked.probability >= threshold);
  if (hasLead) need('lead_real', answers.lead_real.probability, answers.lead_real.probability >= threshold);

  if (failed.length === 0) return { outcome: 'run', reason: `all checks ≥ ${threshold}`, threshold, class: effectiveClass, checks };
  return { outcome: 'hold', reason: `below ${threshold}: ${failed.join(', ')}`, threshold, class: effectiveClass, checks };
}

export function createGuard({ client, rules = DEFAULT_RULES, logFile = null, onLog = null } = {}) {
  rules = { ...DEFAULT_RULES, ...rules, thresholds: { ...DEFAULT_RULES.thresholds, ...(rules.thresholds ?? {}) } };
  if (logFile) mkdirSync(dirname(logFile), { recursive: true });
  let seq = 0;

  function log(entry) {
    if (logFile) appendFileSync(logFile, JSON.stringify(entry) + '\n');
    if (onLog) onLog(entry);
  }

  return {
    rules,
    /** ctx: { goal, history: [...], spentTodayUsd, run } */
    async check(action, ctx) {
      seq += 1;
      const cls = classifyAction(action, rules);
      const cost = Number(action.cost_usd ?? 0);
      const base = { ts: new Date().toISOString(), seq, run: ctx.run ?? null, action: { id: action.id, type: action.type, description: action.description, cost_usd: cost }, class: cls };

      // budget is counted, not asked
      const spent = Number(ctx.spentTodayUsd ?? 0);
      if (!rules.lite && spent + cost > rules.budgetUsd) {
        const entry = { ...base, outcome: 'hold', reason: `budget: $${spent} spent + $${cost} > $${rules.budgetUsd} daily`, answers: null, ms: 0 };
        log(entry);
        return entry;
      }

      const hasLead = Boolean(action.lead);
      const state = buildState(action, ctx, rules);
      const questions = buildQuestions({ hasLead, lite: rules.lite });
      let result;
      try {
        result = await client.decide(state, questions, { timeoutMs: rules.timeoutMs, mockDelayMs: action._mockDelayMs });
      } catch (err) {
        const entry = {
          ...base,
          outcome: 'hold',
          reason: err instanceof JevTimeoutError ? `jev did not answer within ${rules.timeoutMs}ms` : `jev error: ${err.message}`,
          answers: null, ms: null,
        };
        log(entry);
        return entry;
      }
      // "did the last step work" guards against blind retries. If the last step was itself held or
      // denied, a person is already in the loop, so there is nothing to retry blindly.
      const last = (ctx.history ?? []).at(-1);
      const hasHistory = Boolean(last) && !/held|denied/.test(String(last.status ?? ''));
      const verdict = judge(result.answers, cls, rules, { hasLead, hasHistory });
      const entry = {
        ...base,
        class: verdict.class,
        threshold: verdict.threshold,
        outcome: verdict.outcome,
        reason: verdict.reason,
        answers: summarize(result.answers),
        checks: verdict.checks,
        ms: Math.round(result.ms),
        usage: result.usage,
      };
      log(entry);
      return entry;
    },
  };
}

export function summarize(answers) {
  const out = {};
  for (const [k, a] of Object.entries(answers)) {
    if (a.type === 'choice') out[k] = { choice: a.choice, p: a.probability, all: a.probabilities };
    else out[k] = a.probability;
  }
  return out;
}
