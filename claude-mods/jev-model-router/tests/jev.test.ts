import { describe, expect, test } from 'bun:test'
import {
  DEFAULTS,
  buildRequest,
  decisionFromLabel,
  endpoint,
  parseDecision,
  questions,
  resolveBackend,
} from '../hooks/jev.ts'
import { gatewayAnswer, typesafeAnswer } from './harness.ts'

const config = {
  provider: 'auto',
  typesafeApiKey: '',
  gatewayApiKey: '',
  typesafeBaseUrl: '',
  typesafeModel: '',
  gatewayBaseUrl: '',
  gatewayModel: '',
}

describe('resolveBackend', () => {
  test('auto prefers TypeSafe, then the Gateway, then nothing', () => {
    expect(resolveBackend(config)).toBeNull()
    expect(resolveBackend({ ...config, gatewayApiKey: 'g' })?.provider).toBe('gateway')
    expect(resolveBackend({ ...config, gatewayApiKey: 'g', typesafeApiKey: 't' })?.provider).toBe('typesafe')
  })

  test('a forced backend without its key resolves to nothing, never to the other key', () => {
    expect(resolveBackend({ ...config, provider: 'typesafe', gatewayApiKey: 'g' })).toBeNull()
    expect(resolveBackend({ ...config, provider: 'gateway', typesafeApiKey: 't' })).toBeNull()
    expect(resolveBackend({ ...config, provider: 'builtin', typesafeApiKey: 't' })).toBeNull()
  })

  test('each backend keeps its own URL and model, defaults filled in', () => {
    const typesafe = resolveBackend({ ...config, typesafeApiKey: 't' })
    expect(typesafe?.url).toBe('https://api.typesafe.ai/v1/systemone')
    expect(typesafe?.model).toBe(DEFAULTS.typesafe.model)
    const gateway = resolveBackend({
      ...config,
      gatewayApiKey: 'g',
      gatewayBaseUrl: 'https://proxy.example/v4/ai/',
      gatewayModel: 'typesafe-ai/jev-next',
      typesafeBaseUrl: 'https://never.example',
    })
    expect(gateway?.url).toBe('https://proxy.example/v4/ai/evaluation-model')
    expect(gateway?.model).toBe('typesafe-ai/jev-next')
  })

  test('endpoint strips trailing slashes', () => {
    expect(endpoint('typesafe', 'https://a.example///')).toBe('https://a.example/v1/systemone')
  })
})

describe('buildRequest', () => {
  test('TypeSafe carries the model in the body and asks a noul', () => {
    const backend = resolveBackend({ ...config, typesafeApiKey: 'secret' })!
    const { url, init } = buildRequest(backend, { prompt: 'print package.json' })
    expect(url).toBe('https://api.typesafe.ai/v1/systemone')
    expect(init.method).toBe('POST')
    expect(init.headers.authorization).toBe('Bearer secret')
    expect(init.headers['ai-model-id']).toBeUndefined()
    const body = JSON.parse(init.body)
    expect(body.model).toBe('jev-latest')
    expect(body.state).toEqual({ prompt: 'print package.json' })
    expect(body.questions.risk.type).toBe('noul')
    expect(Object.keys(body.questions)).toEqual(['tier', 'effort', 'risk'])
  })

  test('the Gateway carries the model and protocol in headers and asks a boolean', () => {
    const backend = resolveBackend({ ...config, gatewayApiKey: 'secret' })!
    const { url, init } = buildRequest(backend, { prompt: 'x' })
    expect(url).toBe('https://ai-gateway.vercel.sh/v4/ai/evaluation-model')
    expect(init.headers['ai-model-id']).toBe('typesafe-ai/jev')
    expect(init.headers['ai-gateway-auth-method']).toBe('api-key')
    expect(init.headers['ai-gateway-protocol-version']).toBeDefined()
    expect(init.headers['ai-evaluation-model-specification-version']).toBeDefined()
    const body = JSON.parse(init.body)
    expect(body.model).toBeUndefined()
    expect(body.questions.risk.type).toBe('boolean')
  })

  test('the questions describe work, never model names', () => {
    const text = JSON.stringify(questions('typesafe')).toLowerCase()
    for (const word of ['haiku', 'sonnet', 'opus', 'claude']) expect(text).not.toContain(word)
  })
})

describe('parseDecision', () => {
  test('reads a TypeSafe answer with its confidences and noul', () => {
    const decision = parseDecision(typesafeAnswer('fast', 0.97, 0.3, 0.8, 0.04))
    expect(decision).toEqual({
      tier: 'fast',
      tierConfidence: 0.97,
      effort: 0.3,
      effortConfidence: 0.8,
      risk: 0.04,
    })
  })

  test('reads a Gateway answer, deriving confidence from the distribution', () => {
    const decision = parseDecision(gatewayAnswer('balanced', { fast: 0.1, balanced: 0.85, deep: 0.05 }))
    expect(decision?.tier).toBe('balanced')
    expect(decision?.tierConfidence).toBeCloseTo(0.85)
    expect(decision?.effortConfidence).toBeCloseTo(0.9)
    expect(decision?.risk).toBeCloseTo(0.01)
  })

  test('a Gateway answer without a distribution has no confidence', () => {
    expect(parseDecision(gatewayAnswer('deep'))?.tierConfidence).toBeNull()
  })

  test('anything malformed is no decision, never a throw', () => {
    expect(parseDecision('not json')).toBeNull()
    expect(parseDecision('null')).toBeNull()
    expect(parseDecision('{}')).toBeNull()
    expect(parseDecision('{"answers":{}}')).toBeNull()
    expect(parseDecision('{"answers":{"tier":{"choice":"turbo"}}}')).toBeNull()
    expect(parseDecision('{"answers":{"tier":{"choice":"fast","confidence":"high"}}}')).toEqual({
      tier: 'fast',
      tierConfidence: null,
      effort: null,
      effortConfidence: null,
      risk: null,
    })
  })

  test('the built-in classifier yields a decision without confidence', () => {
    expect(decisionFromLabel('deep')?.tier).toBe('deep')
    expect(decisionFromLabel('deep')?.tierConfidence).toBeNull()
    expect(decisionFromLabel(undefined)).toBeNull()
    expect(decisionFromLabel('maybe')).toBeNull()
  })
})
