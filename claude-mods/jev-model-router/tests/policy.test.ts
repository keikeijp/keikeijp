import { describe, expect, test } from 'bun:test'
import type { Decision } from '../hooks/jev.ts'
import {
  PendingDecisions,
  decide,
  effortOfScore,
  effortRank,
  modelIdOf,
  tierRankOf,
} from '../hooks/policy.ts'
import type { Policy } from '../hooks/policy.ts'

const policy: Policy = {
  tiers: { fast: 'haiku', balanced: 'sonnet', deep: 'opus' },
  minUpgradeConfidence: 0.3,
  minDowngradeConfidence: 0.6,
  riskThreshold: 0.7,
}

const said = (tier: Decision['tier'], confidence: number | null, effort: number | null = null, extra: Partial<Decision> = {}): Decision => ({
  tier,
  tierConfidence: confidence,
  effort,
  effortConfidence: effort === null ? null : (extra.effortConfidence ?? confidence),
  risk: 0.02,
  ...extra,
})

describe('ladders', () => {
  test('effort scores round onto the four levels', () => {
    expect(effortOfScore(-1)).toBe('low')
    expect(effortOfScore(0.4)).toBe('low')
    expect(effortOfScore(1.4)).toBe('medium')
    expect(effortOfScore(2.5)).toBe('xhigh')
    expect(effortOfScore(9)).toBe('xhigh')
  })

  test('effort ranks: max sits above the ladder, a number has no rank', () => {
    expect(effortRank('low')).toBe(0)
    expect(effortRank('xhigh')).toBe(3)
    expect(effortRank('max')).toBe(4)
    expect(effortRank(50)).toBeNull()
    expect(effortRank(undefined)).toBeNull()
  })

  test('tier ranks match configured names first, then family words', () => {
    expect(tierRankOf('claude-haiku-4-5-20251001', policy.tiers)).toBe(0)
    expect(tierRankOf('claude-sonnet-5[1m]', policy.tiers)).toBe(1)
    expect(tierRankOf('claude-opus-5', policy.tiers)).toBe(2)
    expect(tierRankOf('claude-fable-5-1', policy.tiers)).toBe(2)
    expect(tierRankOf('gpt-x', policy.tiers)).toBeNull()
    const custom = { fast: 'mini', balanced: 'std', deep: 'big' }
    expect(tierRankOf('acme-std-3', custom)).toBe(1)
  })

  test('aliases resolve to ids for the main loop; ids and unknowns pass through', () => {
    expect(modelIdOf('haiku')).toBe('claude-haiku-4-5-20251001')
    expect(modelIdOf(' Opus ')).toBe('claude-opus-5')
    expect(modelIdOf('claude-sonnet-5[1m]')).toBe('claude-sonnet-5[1m]')
    expect(modelIdOf('my-gateway-model')).toBe('my-gateway-model')
  })
})

describe('decide', () => {
  test('no decision leaves everything alone', () => {
    expect(decide(null, { model: 'claude-opus-5', effort: 'medium' }, policy)).toMatchObject({
      model: null,
      effort: null,
    })
  })

  test('a downgrade needs the high bar', () => {
    const current = { model: 'claude-opus-5', effort: 'medium' }
    expect(decide(said('fast', 0.5, 0.2), current, policy).model).toBeNull()
    expect(decide(said('fast', 0.5, 0.2), current, policy).effort).toBeNull()
    const routed = decide(said('fast', 0.95, 0.2), current, policy)
    expect(routed.model).toBe('haiku')
    expect(routed.effort).toBe('low')
    expect(routed.reason).toContain('fast')
  })

  test('an upgrade needs only the low bar', () => {
    const current = { model: 'claude-haiku-4-5-20251001', effort: 'low' }
    expect(decide(said('deep', 0.25, 2.6), current, policy).model).toBeNull()
    const routed = decide(said('deep', 0.35, 2.6), current, policy)
    expect(routed.model).toBe('opus')
    expect(routed.effort).toBe('xhigh')
  })

  test('the tier the request already runs on is not a change', () => {
    const routed = decide(said('balanced', 0.99, 1), { model: 'claude-sonnet-5[1m]', effort: 'medium' }, policy)
    expect(routed.model).toBeNull()
    expect(routed.effort).toBeNull()
    expect(routed.reason).toContain('kept claude-sonnet-5[1m]/medium')
  })

  test('without a confidence a request may only move up', () => {
    expect(decide(said('fast', null), { model: 'claude-opus-5' }, policy).model).toBeNull()
    expect(decide(said('deep', null), { model: 'claude-haiku-4-5-20251001' }, policy).model).toBe('opus')
  })

  test('an unrecognised model gets the gentler upgrade bar', () => {
    expect(decide(said('fast', 0.4), { model: 'mystery-model' }, policy).model).toBe('haiku')
  })

  test('risk forces the deep tier and at least high effort past both bars', () => {
    const routed = decide(said('fast', 0.99, 0.1, { risk: 0.9 }), { model: 'claude-haiku-4-5-20251001', effort: 'low' }, policy)
    expect(routed.model).toBe('opus')
    expect(routed.effort).toBe('high')
    expect(routed.forced).toBe(true)
    expect(routed.reason).toContain('forced by risk')
  })

  test('risk raises the floor but never lowers an effort already above it', () => {
    const routed = decide(said('fast', 0.99, 0.1, { risk: 0.9 }), { model: 'claude-opus-5', effort: 'max' }, policy)
    expect(routed.model).toBeNull()
    expect(routed.effort).toBeNull()
  })

  test('a numeric effort is the caller\'s own scale and is left alone', () => {
    const routed = decide(said('fast', 0.99, 0.1), { model: 'claude-opus-5', effort: 4000 }, policy)
    expect(routed.model).toBe('haiku')
    expect(routed.effort).toBeNull()
  })

  test('effort can move while the model stays', () => {
    const routed = decide(said('balanced', 0.9, 2.8), { model: 'claude-sonnet-5', effort: 'medium' }, policy)
    expect(routed.model).toBeNull()
    expect(routed.effort).toBe('xhigh')
  })

  test('the risk threshold is configurable', () => {
    const lax = { ...policy, riskThreshold: 0.95 }
    expect(decide(said('fast', 0.99, 0.1, { risk: 0.9 }), { model: 'claude-haiku-4-5-20251001' }, lax).model).toBeNull()
  })
})

describe('PendingDecisions', () => {
  const d = (tier: Decision['tier']): Decision => said(tier, 0.9)

  test('takes by exact text, then by being the only one waiting', () => {
    const pending = new PendingDecisions()
    pending.put('fix the bug', d('balanced'))
    pending.put('print package.json', d('fast'))
    expect(pending.takeSole()).toBeUndefined()
    expect(pending.take('print package.json')?.tier).toBe('fast')
    expect(pending.take('print package.json')).toBeUndefined()
    expect(pending.takeSole()?.tier).toBe('balanced')
    expect(pending.takeSole()).toBeUndefined()
  })

  test('a null decision is remembered as null, not as missing', () => {
    const pending = new PendingDecisions()
    pending.put('x', null)
    expect(pending.take('x')).toBeNull()
  })

  test('holds at most its capacity, dropping the oldest', () => {
    const pending = new PendingDecisions(2)
    pending.put('a', d('fast'))
    pending.put('b', d('fast'))
    pending.put('c', d('fast'))
    expect(pending.size).toBe(2)
    expect(pending.take('a')).toBeUndefined()
    expect(pending.take('c')?.tier).toBe('fast')
  })
})

describe('decide, requests without an effort', () => {
  test('a request that carries no effort gets none, only a model', () => {
    const routed = decide(said('fast', 0.99, 0.1), { model: 'claude-opus-5' }, policy)
    expect(routed.model).toBe('haiku')
    expect(routed.effort).toBeNull()
  })

  test('risk on an already-deep request changes nothing, whatever the id spelling', () => {
    const routed = decide(said('fast', 0.99, 0.1, { risk: 0.95 }), { model: 'claude-opus-5[1m]' }, policy)
    expect(routed.model).toBeNull()
    expect(routed.effort).toBeNull()
  })
})
