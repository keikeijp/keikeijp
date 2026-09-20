/**
 * A stand-in for the engine, enough to drive the hooks without Claude Code:
 * `on` records each hook, `$` answers the few calls the router makes, and
 * `dispatch` runs one hook with a bottom that records what reached it.
 */
import type { Register } from 'claude-code'

export interface HttpResponse {
  status: number
  ok: boolean
  headers: Record<string, string>
  text: string
}

export interface FakeEngineOptions {
  fetch?: (url: string, init: unknown) => Promise<HttpResponse>
  classify?: (text: string, labels: readonly string[]) => Promise<string | undefined>
  /** What `$.session.turns()` answers; the user's prompts so far (1 on a fresh session's first turn). */
  turns?: () => number
}

export function fakeEngine(options: FakeEngineOptions = {}) {
  const logs: string[] = []
  const statuses: (string | undefined)[] = []
  const fetches: { url: string; init: unknown }[] = []
  let now = 1_000
  const $ = {
    session: {
      turns: async () => (options.turns ? options.turns() : 1),
    },
    ui: {
      log: (line: string, opts?: { to?: string }) => {
        if (opts?.to !== 'debug') logs.push(line)
      },
      status: (line: string | undefined) => {
        statuses.push(line)
      },
    },
    clock: {
      now: async () => now,
      sleep: (ms: number) =>
        new Promise<void>((resolve) => {
          setTimeout(resolve, ms)
        }),
    },
    http: {
      fetch: async (url: string, init: unknown) => {
        fetches.push({ url, init })
        now += 250
        if (!options.fetch) throw new Error('no fetch configured')
        return options.fetch(url, init)
      },
    },
    model: {
      classify: async (text: string, labels: readonly string[]) => {
        if (!options.classify) throw new Error('no classifier configured')
        return options.classify(text, labels)
      },
    },
  }
  return { $, logs, statuses, fetches }
}

type Hook = (...args: unknown[]) => unknown

export function load(register: Register, options: Record<string, string | number | boolean>) {
  const hooks = new Map<string, Hook>()
  const on = (event: string, hook: Hook) => {
    hooks.set(event, hook)
    return { off: () => undefined }
  }
  register(on as never, options)

  return {
    events: [...hooks.keys()],
    /** Runs a promise hook; resolves to what reached the bottom of the chain. */
    async dispatch<E>(event: string, $: unknown, e: E): Promise<E> {
      const hook = hooks.get(event)
      if (!hook) return e
      let reached = e
      const next = async (down: E) => {
        reached = down
        return down
      }
      await hook($, e, next)
      return reached
    },
    /** Runs the turn.step generator hook; resolves to what reached the bottom. */
    async step<E extends { turnId: string; index: number }>(
      $: unknown,
      e: E,
    ): Promise<E> {
      const hook = hooks.get('turn.step')
      if (!hook) return e
      let reached = e
      const next = async function* (down: E) {
        reached = down
        return { turnId: down.turnId, index: down.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: null }
      }
      const stream = hook($, e, next) as AsyncGenerator<unknown, unknown>
      let result = await stream.next()
      while (!result.done) result = await stream.next()
      return reached
    },
  }
}

/** A TypeSafe-shaped answer. */
export function typesafeAnswer(
  tier: string,
  confidence: number,
  effort: number,
  effortConfidence = 0.9,
  risk = 0.02,
): string {
  return JSON.stringify({
    answers: {
      tier: { type: 'choice', choice: tier, confidence },
      effort: { type: 'score', score: effort, confidence: effortConfidence },
      risk: { type: 'noul', noul: risk },
    },
  })
}

/** A Gateway-shaped answer, whose confidence is a distribution or nothing. */
export function gatewayAnswer(
  tier: string,
  probabilities?: Record<string, number>,
  effort = 1.4,
  risk = 0.01,
): string {
  return JSON.stringify({
    answers: {
      tier: { type: 'choice', choice: tier, ...(probabilities ? { probabilities } : {}) },
      effort: { type: 'score', score: effort, probabilities: { '1': 0.9 } },
      risk: { type: 'boolean', probability: risk },
    },
  })
}

export const ok = (text: string): HttpResponse => ({ status: 200, ok: true, headers: {}, text })
