/**
 * policy.ts — turns a Jev decision into a model and a reasoning effort.
 *
 * Pure functions only. The router's rules live here so they can be tested
 * without an engine:
 *
 *   - Spending more (a bigger model, more reasoning) needs a low confidence
 *     bar; being wrong costs money.
 *   - Spending less needs a high confidence bar; being wrong starves a task.
 *   - A backend that reports no confidence may only move a request up.
 *   - A risky task (deploys, real money, unrecoverable data) is forced to the
 *     deep tier with at least high effort, past both bars.
 *   - A move whose direction cannot be told (an unrecognised model id, a
 *     numeric effort) is treated as an upgrade, or left alone.
 */
import { TIERS } from './jev.ts'
import type { Decision, Tier } from './jev.ts'

/** The reasoning levels the router may ask for, cheapest first. */
export const EFFORTS = ['low', 'medium', 'high', 'xhigh'] as const

export type Effort = (typeof EFFORTS)[number]

export type Tiers = Record<Tier, string>

export interface Policy {
  tiers: Tiers
  minUpgradeConfidence: number
  minDowngradeConfidence: number
  riskThreshold: number
}

/** What a request currently asks for, as `turn.step` or `agent.spawn` hands it over. */
export interface Current {
  model: string
  effort?: string | number
}

export interface Routing {
  /** The model to switch to, or null to leave it. An alias or an id as configured. */
  model: string | null
  /** The effort to ask for, or null to leave it. */
  effort: Effort | null
  /** True when risk overrode the confidence bars. */
  forced: boolean
  /** Why, for the log. */
  reason: string
}

export const NO_ROUTING: Routing = { model: null, effort: null, forced: false, reason: 'no decision' }

/** The rubric score (0..3) as a reasoning level. */
export function effortOfScore(score: number): Effort {
  const index = Math.min(EFFORTS.length - 1, Math.max(0, Math.round(score)))
  return EFFORTS[index] as Effort
}

/**
 * Where a reasoning level sits on the ladder. `max` ranks above every rung
 * the rubric can produce without being one the router asks for. A number is
 * the caller's own scale, so it has no rank here.
 */
export function effortRank(effort: string | number | undefined): number | null {
  if (typeof effort !== 'string') return null
  if (effort === 'max') return EFFORTS.length
  const index = (EFFORTS as readonly string[]).indexOf(effort)
  return index === -1 ? null : index
}

/**
 * Where a model sits on the tier ladder: matched against the configured tier
 * names first, then the family words. Null for a model matching none.
 */
export function tierRankOf(model: string, tiers: Tiers): number | null {
  const lowered = model.toLowerCase()
  for (let index = 0; index < TIERS.length; index++) {
    const configured = tiers[TIERS[index] as Tier].toLowerCase()
    if (configured && lowered.includes(configured)) return index
  }
  if (lowered.includes('haiku')) return 0
  if (lowered.includes('sonnet')) return 1
  if (lowered.includes('opus') || lowered.includes('fable')) return 2
  return null
}

/**
 * Whether a move from `current` to `wanted` clears its bar. A move whose
 * direction is unknown counts as an upgrade; a decision without confidence
 * clears the upgrade bar only.
 */
export function clearsBar(
  wanted: number,
  current: number | null,
  confidence: number | null,
  policy: Policy,
): boolean {
  if (current !== null && wanted === current) return false
  const downgrade = current !== null && wanted < current
  if (confidence === null) return !downgrade
  return confidence >= (downgrade ? policy.minDowngradeConfidence : policy.minUpgradeConfidence)
}

/** Turns a decision into a routing for one request. */
export function decide(decision: Decision | null, current: Current, policy: Policy): Routing {
  if (!decision) return NO_ROUTING

  let tier = decision.tier
  let effortScore = decision.effort
  const forced = decision.risk !== null && decision.risk > policy.riskThreshold
  if (forced) {
    tier = 'deep'
    effortScore = Math.max(effortScore ?? 0, 2)
  }

  const wantedModel = policy.tiers[tier]
  const wantedTierRank = TIERS.indexOf(tier)
  const currentTierRank = tierRankOf(current.model, policy.tiers)
  // The tier a request already runs on is never a change, however the id is
  // spelled (`claude-opus-5[1m]` stays on its 1M-context id), and risk skips
  // the bars, not this.
  const model =
    wantedModel &&
    wantedModel !== current.model &&
    wantedTierRank !== currentTierRank &&
    (forced || clearsBar(wantedTierRank, currentTierRank, decision.tierConfidence, policy))
      ? wantedModel
      : null

  // Effort is routed only where the request carries one on the ladder: a
  // number is the caller's own scale, and a request without effort (a model
  // that takes none, a subagent spawn) has nothing to move.
  let effort: Effort | null = null
  if (effortScore !== null && typeof current.effort === 'string') {
    const currentRank = effortRank(current.effort)
    let wantedRank = EFFORTS.indexOf(effortOfScore(effortScore))
    // Risk raises the floor; it never lowers a level already above it.
    if (forced && currentRank !== null) wantedRank = Math.max(wantedRank, currentRank)
    if (
      wantedRank !== currentRank &&
      wantedRank < EFFORTS.length &&
      (forced || clearsBar(wantedRank, currentRank, decision.effortConfidence, policy))
    ) {
      effort = EFFORTS[wantedRank] as Effort
    }
  }

  const said =
    decision.tierConfidence === null ? 'confidence n/a' : `confidence ${decision.tierConfidence.toFixed(2)}`
  if (!model && !effort) {
    const kept = current.effort === undefined ? current.model : `${current.model}/${current.effort}`
    const wantedEffort = effortScore === null ? '' : `/${effortOfScore(effortScore)}`
    return {
      model: null,
      effort: null,
      forced,
      reason: `kept ${kept}, wanted ${wantedModel}${wantedEffort} (${said})`,
    }
  }
  return { model, effort, forced, reason: forced ? `${tier}, forced by risk` : `${tier} (${said})` }
}

/**
 * The id behind a family alias. `agent.spawn` takes an alias the way the
 * Agent tool does, but `turn.step`'s `model` goes to the API as written, so
 * a main-loop rewrite needs the id. Anything unrecognised is passed through.
 */
export const ALIAS_IDS: Record<string, string> = {
  haiku: 'claude-haiku-4-5-20251001',
  sonnet: 'claude-sonnet-5',
  opus: 'claude-opus-5',
  fable: 'claude-fable-5-1',
}

export function modelIdOf(model: string): string {
  return ALIAS_IDS[model.trim().toLowerCase()] ?? model
}

/**
 * Holds each prompt's decision from `prompt.submit` until the turn that runs
 * that prompt starts. Keyed by the prompt's text: `turn.start` carries the
 * same text, so a queued prompt gets its own decision rather than another's.
 * `takeSole` is the fallback for a turn whose text matched nothing: the only
 * decision waiting is taken, and with several waiting none is, since routing
 * a turn on another prompt's decision is worse than not routing it.
 */
export class PendingDecisions {
  private readonly held = new Map<string, Decision | null>()

  constructor(private readonly capacity = 8) {}

  put(text: string, decision: Decision | null): void {
    this.held.delete(text)
    this.held.set(text, decision)
    while (this.held.size > this.capacity) {
      const oldest = this.held.keys().next().value
      if (oldest === undefined) break
      this.held.delete(oldest)
    }
  }

  /** The decision stored for exactly this text, or undefined when there is none. */
  take(text: string): Decision | null | undefined {
    if (!this.held.has(text)) return undefined
    const decision = this.held.get(text)
    this.held.delete(text)
    return decision
  }

  /** The only decision waiting, or undefined when none or several wait. */
  takeSole(): Decision | null | undefined {
    if (this.held.size !== 1) return undefined
    const [key, decision] = this.held.entries().next().value as [string, Decision | null]
    this.held.delete(key)
    return decision
  }

  clear(): void {
    this.held.clear()
  }

  get size(): number {
    return this.held.size
  }
}
