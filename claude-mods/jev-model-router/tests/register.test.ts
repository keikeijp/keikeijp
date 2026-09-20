import { describe, expect, test } from 'bun:test'
import { register } from '../hooks/register.ts'
import { fakeEngine, gatewayAnswer, load, ok, typesafeAnswer } from './harness.ts'

const OPUS = 'claude-opus-5'
const HAIKU = 'claude-haiku-4-5-20251001'

const base = { typesafeApiKey: 'ts-key', timeoutMs: 500 }

const step = (turnId: string, index: number, messageCount: number, model = OPUS, effort = 'medium') => ({
  turnId,
  index,
  model,
  effort,
  messageCount,
})

const submit = (text: string) => ({ text, wait: false, origin: { kind: 'composer' as const } })

/** One full main-loop turn: prompt.submit, turn.start, then the first step. */
async function turn(
  mod: ReturnType<typeof load>,
  $: unknown,
  text: string,
  turnId: string,
  messageCount: number,
  current: { model?: string; effort?: string } = {},
) {
  await mod.dispatch('prompt.submit', $, submit(text))
  await mod.dispatch('turn.start', $, { text, turnId })
  return mod.step($, step(turnId, 0, messageCount, current.model, current.effort))
}

describe('register', () => {
  test('hooks the five events', () => {
    const mod = load(register, base)
    expect(mod.events.sort()).toEqual(['agent.spawn', 'prompt.submit', 'session.start', 'turn.start', 'turn.step'])
  })

  test('the first prompt routes the main model and effort; later steps of the turn reuse it', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('fast', 1, 0.1, 0.95, 0.03)) })
    const mod = load(register, base)
    await mod.dispatch('session.start', engine.$, { cwd: '/w', surface: 'terminal', isInteractive: true })

    const first = await turn(mod, engine.$, 'print the contents of package.json', 't1', 1)
    expect(first.model).toBe(HAIKU)
    expect(first.effort).toBe('low')

    const second = await mod.step(engine.$, step('t1', 1, 3))
    expect(second.model).toBe(HAIKU)
    expect(second.effort).toBe('low')

    expect(engine.fetches).toHaveLength(1)
    expect(engine.fetches[0]?.url).toBe('https://api.typesafe.ai/v1/systemone')
    expect(JSON.parse((engine.fetches[0]?.init as { body: string }).body).state).toEqual({
      prompt: 'print the contents of package.json',
    })
    expect(engine.logs[0]).toContain('ready on typesafe (https://api.typesafe.ai/v1/systemone)')
    expect(engine.logs[0]).toContain('main model (session-start)')
    expect(engine.logs.some((l) => l.includes('jev: tier fast (1.00) · effort 0.1 → low (0.95) · risky 0.03'))).toBe(true)
    expect(engine.logs.some((l) => l.includes(`main loop → ${HAIKU}, effort low: fast (confidence 1.00)`))).toBe(true)
    expect(engine.statuses.at(-1)).toBe(`jev · fast 1.00 → ${HAIKU}/low`)
  })

  test('after the first turn the main model is pinned but the effort still moves', async () => {
    let answer = typesafeAnswer('fast', 1, 0.1)
    const engine = fakeEngine({ fetch: async () => ok(answer) })
    const mod = load(register, base)
    await mod.dispatch('session.start', engine.$, { cwd: '/w', surface: 'terminal', isInteractive: true })
    await turn(mod, engine.$, 'first', 't1', 1)

    answer = typesafeAnswer('deep', 0.9, 2.7)
    const routed = await turn(mod, engine.$, 'redesign the auth layer', 't2', 5, { model: HAIKU, effort: 'low' })
    expect(routed.model).toBe(HAIKU)
    expect(routed.effort).toBe('xhigh')
    expect(engine.logs.at(-1)).toContain('main loop → effort xhigh: deep (confidence 0.90) (main model is picked at session start only)')
  })

  test('a resumed session keeps its model for the prompt cache', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('fast', 1, 0.1)), turns: () => 12 })
    const mod = load(register, base)
    await mod.dispatch('session.start', engine.$, { cwd: '/w', surface: 'terminal', isInteractive: true })
    const routed = await turn(mod, engine.$, 'continue', 't1', 40)
    expect(routed.model).toBe(OPUS)
    expect(routed.effort).toBe('low')
    expect(engine.logs.at(-1)).toContain('resumed session, main model kept')
  })

  test('every-turn re-routes the main model on each turn', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('fast', 1, 0.1)) })
    const mod = load(register, { ...base, mainModelRouting: 'every-turn' })
    await turn(mod, engine.$, 'a', 't1', 1)
    const routed = await turn(mod, engine.$, 'b', 't2', 9)
    expect(routed.model).toBe(HAIKU)
  })

  test('a suppressed model change with no effort change is reported as kept', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('deep', 0.9, 1)) })
    const mod = load(register, { ...base, mainModelRouting: 'off' })
    const routed = await turn(mod, engine.$, 'a', 't1', 1, { model: HAIKU, effort: 'medium' })
    expect(routed.model).toBe(HAIKU)
    expect(routed.effort).toBe('medium')
    expect(engine.logs.at(-1)).toContain('main loop kept as is, wanted opus: deep (confidence 0.90) (main model routing off)')
  })

  test('off never touches the main model, and says so', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('fast', 1, 0.1)) })
    const mod = load(register, { ...base, mainModelRouting: 'off' })
    const routed = await turn(mod, engine.$, 'a', 't1', 1)
    expect(routed.model).toBe(OPUS)
    expect(routed.effort).toBe('low')
    expect(engine.logs.at(-1)).toContain('main model routing off')
  })

  test('a decision below the downgrade bar leaves the turn alone but is reported', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('fast', 0.41, 0.4, 0.38)) })
    const mod = load(register, base)
    const routed = await turn(mod, engine.$, 'hmm', 't1', 1)
    expect(routed).toEqual(step('t1', 0, 1))
    expect(engine.logs.at(-1)).toContain(`main loop: kept ${OPUS}/medium, wanted haiku/low (confidence 0.41)`)
    expect(engine.statuses.at(-1)).toBe('jev · fast 0.41 · unchanged')
  })

  test('a slow backend is left behind at the budget and the turn goes through unchanged', async () => {
    const engine = fakeEngine({
      fetch: () => new Promise((resolve) => setTimeout(() => resolve(ok(typesafeAnswer('fast', 1, 0))), 200)),
    })
    const mod = load(register, { ...base, timeoutMs: 20 })
    const routed = await turn(mod, engine.$, 'slow', 't1', 1)
    expect(routed).toEqual(step('t1', 0, 1))
    expect(engine.logs.some((l) => l.includes('passed the 20ms budget'))).toBe(true)
  })

  test('a non-2xx, a malformed body and a thrown error are all fail-open', async () => {
    for (const fetch of [
      async () => ({ status: 401, ok: false, headers: {}, text: 'nope' }),
      async () => ok('<html>'),
      async () => {
        throw new Error('ECONNRESET')
      },
    ]) {
      const engine = fakeEngine({ fetch })
      const mod = load(register, base)
      const routed = await turn(mod, engine.$, 'x', 't1', 1)
      expect(routed).toEqual(step('t1', 0, 1))
      expect(engine.statuses.at(-1)).toBe('jev · no answer')
    }
  })

  test('the Gateway backend sends its headers and reads its answer shape', async () => {
    const engine = fakeEngine({ fetch: async () => ok(gatewayAnswer('deep', { fast: 0.05, balanced: 0.15, deep: 0.8 }, 2.6)) })
    const mod = load(register, { gatewayApiKey: 'gw' })
    const routed = await turn(mod, engine.$, 'design the migration', 't1', 1, { model: HAIKU, effort: 'low' })
    expect(engine.fetches[0]?.url).toBe('https://ai-gateway.vercel.sh/v4/ai/evaluation-model')
    expect((engine.fetches[0]?.init as { headers: Record<string, string> }).headers['ai-model-id']).toBe('typesafe-ai/jev')
    expect(routed.model).toBe(OPUS)
    expect(routed.effort).toBe('xhigh')
  })

  test('without a key the built-in classifier answers, and may only move a request up', async () => {
    const seen: string[] = []
    const engine = fakeEngine({
      classify: async (text, labels) => {
        seen.push(text)
        expect(labels).toEqual(['fast', 'balanced', 'deep'])
        return 'fast'
      },
    })
    const mod = load(register, {})
    const kept = await turn(mod, engine.$, 'cheap task', 't1', 1)
    expect(kept.model).toBe(OPUS)
    expect(engine.fetches).toHaveLength(0)
    expect(seen).toEqual(['cheap task'])
    expect(engine.logs[0]).toContain('ready on the built-in classifier, no key set')
  })

  test('a forced backend without its key says so once and degrades to the built-in classifier', async () => {
    const engine = fakeEngine({ classify: async () => 'deep' })
    const mod = load(register, { provider: 'gateway' })
    await turn(mod, engine.$, 'a', 't1', 1, { model: HAIKU })
    await turn(mod, engine.$, 'b', 't2', 3, { model: HAIKU })
    expect(engine.logs.filter((l) => l.includes('provider "gateway" has no key set'))).toHaveLength(1)
  })

  test('with every switch off nothing is classified, but the router still announces itself', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('fast', 1, 0)) })
    const mod = load(register, { ...base, routeMainEffort: false, mainModelRouting: 'off', routeSubagentModel: false })
    const routed = await turn(mod, engine.$, 'a', 't1', 1)
    expect(routed).toEqual(step('t1', 0, 1))
    expect(engine.fetches).toHaveLength(0)
    expect(engine.logs[0]).toContain('routing nothing, every switch is off')
  })

  test('a queued prompt is routed on its own decision, not the other prompt\'s', async () => {
    const byPrompt: Record<string, string> = {
      'print package.json': typesafeAnswer('fast', 1, 0.1),
      'refactor the scheduler': typesafeAnswer('deep', 0.9, 2.8),
    }
    const engine = fakeEngine({
      fetch: async (_url, init) => ok(byPrompt[JSON.parse((init as { body: string }).body).state.prompt] ?? '{}'),
    })
    const mod = load(register, { ...base, mainModelRouting: 'every-turn' })
    await mod.dispatch('prompt.submit', engine.$, submit('print package.json'))
    await mod.dispatch('prompt.submit', engine.$, submit('refactor the scheduler'))

    await mod.dispatch('turn.start', engine.$, { text: 'refactor the scheduler', turnId: 't2' })
    const hard = await mod.step(engine.$, step('t2', 0, 3, HAIKU, 'low'))
    expect(hard.model).toBe(OPUS)
    expect(hard.effort).toBe('xhigh')

    await mod.dispatch('turn.start', engine.$, { text: 'print package.json', turnId: 't1' })
    const easy = await mod.step(engine.$, step('t1', 0, 5, OPUS, 'high'))
    expect(easy.model).toBe(HAIKU)
    expect(easy.effort).toBe('low')
  })

  test('a step in a subagent loop is never touched', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('fast', 1, 0.1)) })
    const mod = load(register, base)
    await mod.dispatch('prompt.submit', engine.$, submit('a'))
    const routed = await mod.step(engine.$, { ...step('t1', 0, 1), agentId: 'agent-7' })
    expect(routed.model).toBe(OPUS)
    expect(routed.effort).toBe('medium')
  })

  test('agent.spawn routes the subagent model from its prompt, description and type', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('fast', 0.98, 0.2)) })
    const mod = load(register, base)
    const spawn = {
      tool_use_id: 'toolu_1',
      prompt: 'list the files under src',
      description: 'List src files',
      subagentType: 'Explore',
      provider: { plugin: 'engine', tier: 'core' },
      parentModel: OPUS,
      background: false,
      fork: false,
    }
    const routed = await mod.dispatch('agent.spawn', engine.$, spawn)
    expect(routed.model).toBe('haiku')
    const state = JSON.parse((engine.fetches[0]?.init as { body: string }).body).state
    expect(state).toEqual({ prompt: 'list the files under src', description: 'List src files', agentType: 'Explore' })
    expect(engine.logs.some((l) => l.includes('jev (Explore): tier fast (0.98)'))).toBe(true)
    expect(engine.logs.at(-1)).toContain('Explore → haiku: fast (confidence 0.98)')
  })

  test('agent.spawn measures a change from the model the caller named', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('fast', 0.98, 0.2)) })
    const mod = load(register, base)
    const spawn = {
      tool_use_id: 'toolu_1',
      prompt: 'x',
      description: 'y',
      subagentType: 'general-purpose',
      provider: { plugin: 'engine', tier: 'core' },
      model: 'haiku',
      parentModel: OPUS,
      background: false,
      fork: false,
    }
    const routed = await mod.dispatch('agent.spawn', engine.$, spawn)
    expect(routed.model).toBe('haiku')
    expect(engine.logs.at(-1)).toContain('kept haiku')
  })

  test('a fork and a disabled switch leave agent.spawn alone without classifying', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('fast', 1, 0)) })
    const spawn = {
      tool_use_id: 'toolu_1',
      prompt: 'x',
      description: 'y',
      subagentType: 'fork',
      provider: { plugin: 'engine', tier: 'core' },
      parentModel: OPUS,
      background: false,
      fork: true,
    }
    expect((await load(register, base).dispatch('agent.spawn', engine.$, spawn)).model).toBeUndefined()
    expect(
      (await load(register, { ...base, routeSubagentModel: false }).dispatch('agent.spawn', engine.$, { ...spawn, fork: false })).model,
    ).toBeUndefined()
    expect(engine.fetches).toHaveLength(0)
  })

  test('logDecisions off keeps the transcript and status line silent', async () => {
    const engine = fakeEngine({ fetch: async () => ok(typesafeAnswer('fast', 1, 0.1)) })
    const mod = load(register, { ...base, logDecisions: false })
    const routed = await turn(mod, engine.$, 'a', 't1', 1)
    expect(routed.model).toBe(HAIKU)
    expect(engine.logs).toEqual([])
    expect(engine.statuses).toEqual([])
  })
})
