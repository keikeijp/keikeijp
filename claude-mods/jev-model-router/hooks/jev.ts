/**
 * jev.ts — the wire layer.
 *
 * Pure functions only: no `$`, no I/O. This module builds the HTTP request
 * that asks Jev about a task and reads the typed answer it sends back.
 *
 * Jev (TypeSafe's "System One" decision model) does not generate text: it is
 * given a `state` and a map of typed `questions`, and answers each question
 * with a choice, a score or a probability, with a confidence attached.
 *
 * The same model is reachable through two backends with two wire shapes:
 *
 *   typesafe  POST {base}/v1/systemone
 *             body `{ model, state, questions }`, bearer auth;
 *             a yes/no question is `noul`; every answer carries `confidence`.
 *   gateway   POST {base}/evaluation-model
 *             body `{ state, questions }`, model and protocol in headers;
 *             a yes/no question is `boolean`; there is no `confidence`, only
 *             an optional probability distribution to derive one from.
 */

export type Provider = 'typesafe' | 'gateway'

export type Tier = 'fast' | 'balanced' | 'deep'

/** Every tier, cheapest first. Also the label set for the built-in classifier. */
export const TIERS: readonly Tier[] = ['fast', 'balanced', 'deep']

/** What Jev said about one task, before any policy is applied. */
export interface Decision {
  tier: Tier
  /** Confidence in `tier` (0..1), or null when the backend reported none. */
  tierConfidence: number | null
  /** 0..3 on the effort rubric, or null when the answer was missing. */
  effort: number | null
  /** Confidence in `effort`, or null when the backend reported none. */
  effortConfidence: number | null
  /** P(carrying out the task is itself costly or irreversible), or null. */
  risk: number | null
}

export interface Backend {
  provider: Provider
  url: string
  model: string
  apiKey: string
}

export interface BackendConfig {
  provider: string
  typesafeApiKey: string
  gatewayApiKey: string
  typesafeBaseUrl: string
  typesafeModel: string
  gatewayBaseUrl: string
  gatewayModel: string
}

export const DEFAULTS = {
  typesafe: { baseUrl: 'https://api.typesafe.ai', path: '/v1/systemone', model: 'jev-latest' },
  gateway: {
    baseUrl: 'https://ai-gateway.vercel.sh/v4/ai',
    path: '/evaluation-model',
    model: 'typesafe-ai/jev',
  },
} as const

/** The Gateway refuses a request that does not name the protocol it speaks. */
const GATEWAY_PROTOCOL_VERSION = '0.0.1'
const GATEWAY_EVALUATION_SPEC_VERSION = '4'

/**
 * How each tier is described to Jev. About the shape of the work, never
 * about model names: the decision model never sees a model id.
 */
export const TIER_CRITERIA: Record<Tier, string> = {
  fast: 'Mechanical and local: read or print a file, run one command, rename a symbol, answer something already in context, a one-line edit.',
  balanced:
    'Ordinary engineering: implement a well-specified change across a few files, write or fix tests, fix a clearly described bug, review a small diff.',
  deep: 'Hard or high-stakes: architecture and design, debugging a failure with an unknown cause, security, data migrations, concurrency, performance work, anything touching production or money.',
}

/** The effort rubric, index 0..3, mapped onto low / medium / high / xhigh. */
export const EFFORT_RUBRIC = ['almost none', 'some', 'a lot', 'as much as possible'] as const

/** Picks the backend a configuration asks for; null means the built-in classifier. */
export function resolveBackend(config: BackendConfig): Backend | null {
  const forced = config.provider.trim().toLowerCase() || 'auto'
  const provider: Provider | null =
    forced === 'builtin'
      ? null
      : forced === 'typesafe'
        ? config.typesafeApiKey
          ? 'typesafe'
          : null
        : forced === 'gateway'
          ? config.gatewayApiKey
            ? 'gateway'
            : null
          : config.typesafeApiKey
            ? 'typesafe'
            : config.gatewayApiKey
              ? 'gateway'
              : null
  if (!provider) return null
  if (provider === 'typesafe') {
    return {
      provider,
      apiKey: config.typesafeApiKey,
      model: config.typesafeModel || DEFAULTS.typesafe.model,
      url: endpoint('typesafe', config.typesafeBaseUrl || DEFAULTS.typesafe.baseUrl),
    }
  }
  return {
    provider,
    apiKey: config.gatewayApiKey,
    model: config.gatewayModel || DEFAULTS.gateway.model,
    url: endpoint('gateway', config.gatewayBaseUrl || DEFAULTS.gateway.baseUrl),
  }
}

/** The full URL a backend posts to. */
export function endpoint(provider: Provider, baseUrl: string): string {
  return baseUrl.replace(/\/+$/, '') + DEFAULTS[provider].path
}

/** The three questions, in the vocabulary the backend's schema uses. */
export function questions(provider: Provider): Record<string, unknown> {
  return {
    tier: {
      type: 'choice',
      instructions: 'Which is the cheapest tier that can complete this coding task well?',
      criteria: TIER_CRITERIA,
    },
    effort: {
      type: 'score',
      instructions: 'How much step-by-step reasoning does this task need?',
      criteria: EFFORT_RUBRIC,
    },
    risk: {
      type: provider === 'typesafe' ? 'noul' : 'boolean',
      // Asked about the act, not the subject: code that is *about* money is
      // ordinary code; deploying to production or deleting data is not.
      instructions:
        'Carrying out this task would itself change a production system, move real money, or alter data that cannot be restored. Writing or testing code that merely deals with such things does not count.',
    },
  }
}

export interface JevRequest {
  url: string
  init: { method: 'POST'; headers: Record<string, string>; body: string }
}

/** The request that asks Jev about `state`, in the backend's wire shape. */
export function buildRequest(backend: Backend, state: Record<string, unknown>): JevRequest {
  const common = {
    'content-type': 'application/json',
    authorization: `Bearer ${backend.apiKey}`,
  }
  if (backend.provider === 'typesafe') {
    return {
      url: backend.url,
      init: {
        method: 'POST',
        headers: common,
        body: JSON.stringify({ model: backend.model, state, questions: questions('typesafe') }),
      },
    }
  }
  return {
    url: backend.url,
    init: {
      method: 'POST',
      headers: {
        ...common,
        'ai-gateway-auth-method': 'api-key',
        'ai-model-id': backend.model,
        'ai-gateway-protocol-version': GATEWAY_PROTOCOL_VERSION,
        'ai-evaluation-model-specification-version': GATEWAY_EVALUATION_SPEC_VERSION,
      },
      body: JSON.stringify({ state, questions: questions('gateway') }),
    },
  }
}

type Answer = Record<string, unknown>

function isTier(value: unknown): value is Tier {
  return value === 'fast' || value === 'balanced' || value === 'deep'
}

function number(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/**
 * How sure an answer is: the backend's own `confidence` when it reports one,
 * else the highest probability of its distribution, else unknown.
 */
function confidenceOf(answer: Answer | undefined): number | null {
  if (!answer) return null
  const reported = number(answer.confidence)
  if (reported !== null) return reported
  const distribution = answer.probabilities
  if (distribution && typeof distribution === 'object') {
    const values = Object.values(distribution as Record<string, unknown>)
      .map(number)
      .filter((v): v is number => v !== null)
    if (values.length > 0) return Math.max(...values)
  }
  return null
}

/**
 * Reads either backend's response. Anything malformed reads as no decision,
 * never as a throw: the caller treats null as "leave the request alone".
 */
export function parseDecision(text: string): Decision | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object') return null
  const answers = (parsed as { answers?: Record<string, Answer> }).answers
  if (!answers || typeof answers !== 'object') return null

  const tier = answers.tier
  const choice = tier?.choice ?? tier?.answer ?? tier?.value
  if (!isTier(choice)) return null

  const effort = answers.effort
  const risk = answers.risk
  return {
    tier: choice,
    tierConfidence: confidenceOf(tier),
    effort: number(effort?.score ?? effort?.value),
    effortConfidence: effort ? confidenceOf(effort) : null,
    risk: number(risk?.noul ?? risk?.probability ?? risk?.value),
  }
}

/** A decision from a plain label, as the built-in classifier answers. */
export function decisionFromLabel(label: string | undefined): Decision | null {
  if (!isTier(label)) return null
  return { tier: label, tierConfidence: null, effort: null, effortConfidence: null, risk: null }
}
