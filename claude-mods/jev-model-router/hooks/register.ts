/**
 * jev-model-router — a Claude Code mod (function hooks, early access).
 *
 * On every request, TypeSafe's Jev decides how much model the task deserves,
 * and this module applies the answer in three places:
 *
 *   agent.spawn     the model of each subagent            (routeSubagentModel)
 *   turn.step       the reasoning effort of the main loop (routeMainEffort)
 *   turn.step       the model of the main loop, once, at session start
 *                   (mainModelRouting: session-start | every-turn | off)
 *
 * The main model is picked from the first prompt only, before any prompt
 * cache exists: switching it later invalidates the cache, which on a long
 * context costs more than the cheaper tier saves. Effort is a per-request
 * parameter and does not touch the cache, so it moves on every turn.
 *
 * Jev is reached through TypeSafe's direct API or the Vercel AI Gateway,
 * whichever key is configured; with neither, the engine's own
 * `$.model.classify` answers the tier question without a confidence.
 *
 * Every failure is fail-open: a timeout, a non-2xx, a malformed body or a
 * thrown error leaves the request exactly as the engine built it.
 *
 * Needs CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 and Claude Code 2.1.259+.
 * Privacy: with a key set, the prompt text (and a subagent's prompt,
 * description and type) is sent to the backend that key belongs to.
 *
 * Shape note: `claude plugin validate` reads every call on `$` off this
 * file, so `$` is only ever passed to functions declared at its top level.
 */
import type { EngineInterface, PluginOptions, Register } from 'claude-code'
import { TIERS, buildRequest, decisionFromLabel, parseDecision, resolveBackend } from './jev.ts'
import type { Backend, Decision } from './jev.ts'
import { PendingDecisions, decide, modelIdOf } from './policy.ts'
import type { Policy } from './policy.ts'
import { TAG, describeDecision, describeMainRouting, describeSetup, describeStatus } from './report.ts'
import type { Change, Switches } from './report.ts'

type MainModelRouting = Switches['mainModel']

/** How many turns' applied changes are remembered for their later steps. */
const APPLIED_CAPACITY = 32

/** Everything read from the plugin's options, once, at register. */
interface Settings {
  provider: string
  backend: Backend | null
  builtinByChoice: boolean
  keyMissing: boolean
  policy: Policy
  routeSubagentModel: boolean
  routeMainEffort: boolean
  mainModelRouting: MainModelRouting
  timeoutMs: number
  logDecisions: boolean
}

/** What the router remembers across events, per process. */
interface State {
  announced: boolean
  /** Decisions made at prompt.submit, waiting for their turn. */
  pending: PendingDecisions
  /** A turn's decision, bound at turn.start, read at its first step. */
  byTurn: Map<string, Decision | null>
  /** What each turn's first step settled on, reused by its later steps. */
  applied: Map<string, Change | null>
  /** Open until the first main-loop request of the process: the one time
   * the main model may be switched without a prompt cache to lose. */
  mainModelWindow: boolean
}

function mainModelRoutingOf(value: string): MainModelRouting {
  const lowered = value.trim().toLowerCase()
  return lowered === 'every-turn' || lowered === 'off' ? lowered : 'session-start'
}

function readSettings(options: PluginOptions): Settings {
  const text = (key: string, fallback: string): string =>
    typeof options[key] === 'string' ? (options[key] as string) : fallback
  const number = (key: string, fallback: number): number =>
    typeof options[key] === 'number' ? (options[key] as number) : fallback
  const flag = (key: string, fallback: boolean): boolean =>
    typeof options[key] === 'boolean' ? (options[key] as boolean) : fallback

  const provider = text('provider', 'auto').trim().toLowerCase() || 'auto'
  const backend = resolveBackend({
    provider,
    typesafeApiKey: text('typesafeApiKey', ''),
    gatewayApiKey: text('gatewayApiKey', ''),
    typesafeBaseUrl: text('typesafeBaseUrl', ''),
    typesafeModel: text('typesafeModel', ''),
    gatewayBaseUrl: text('gatewayBaseUrl', ''),
    gatewayModel: text('gatewayModel', ''),
  })
  const builtinByChoice = provider === 'builtin'
  return {
    provider,
    backend,
    builtinByChoice,
    keyMissing: !backend && !builtinByChoice && provider !== 'auto',
    policy: {
      tiers: {
        fast: text('fastModel', 'haiku'),
        balanced: text('balancedModel', 'sonnet'),
        deep: text('deepModel', 'opus'),
      },
      minUpgradeConfidence: number('minUpgradeConfidence', 0.3),
      minDowngradeConfidence: number('minDowngradeConfidence', 0.6),
      riskThreshold: number('riskThreshold', 0.7),
    },
    routeSubagentModel: flag('routeSubagentModel', true),
    routeMainEffort: flag('routeMainEffort', true),
    mainModelRouting: mainModelRoutingOf(text('mainModelRouting', 'session-start')),
    timeoutMs: Math.max(1, number('timeoutMs', 800)),
    logDecisions: flag('logDecisions', true),
  }
}

function freshState(): State {
  return {
    announced: false,
    pending: new PendingDecisions(),
    byTurn: new Map(),
    applied: new Map(),
    mainModelWindow: true,
  }
}

function log($: EngineInterface, settings: Settings, line: string): void {
  if (settings.logDecisions) $.ui.log(`${TAG} ${line}`)
}

/**
 * Said once, the first time any hook runs: a router that loaded and one that
 * never loaded are otherwise told apart only by later lines, and the policy
 * leaves most requests alone.
 */
function announce($: EngineInterface, settings: Settings, state: State): void {
  if (state.announced) return
  state.announced = true
  log(
    $,
    settings,
    describeSetup(
      settings.backend,
      {
        subagentModel: settings.routeSubagentModel,
        mainEffort: settings.routeMainEffort,
        mainModel: settings.mainModelRouting,
      },
      settings.builtinByChoice,
    ),
  )
  if (settings.keyMissing) {
    log($, settings, `provider "${settings.provider}" has no key set; using the built-in classifier`)
  }
}

/** Asks Jev (or the built-in classifier) about `state`; null on any failure. */
async function classify(
  $: EngineInterface,
  settings: Settings,
  subject: string,
  state: Record<string, string>,
): Promise<{ decision: Decision | null; ms: number }> {
  const startedAt = await $.clock.now()
  let decision: Decision | null = null
  const backend = settings.backend
  if (backend) {
    try {
      const { url, init } = buildRequest(backend, state)
      const request = $.http.fetch(url, init)
      // A rejection after the budget expired must not surface as unhandled.
      request.catch(() => undefined)
      const response = await Promise.race([request, $.clock.sleep(settings.timeoutMs)])
      if (!response) {
        log($, settings, `Jev passed the ${settings.timeoutMs}ms budget; leaving ${subject} alone`)
      } else if (!response.ok) {
        log($, settings, `${backend.provider} responded ${response.status}; leaving ${subject} alone`)
      } else {
        decision = parseDecision(response.text)
        if (!decision) log($, settings, `${backend.provider} answered without a tier; leaving ${subject} alone`)
      }
    } catch (error) {
      log($, settings, `classification failed: ${String(error)}`)
    }
  } else {
    try {
      const subjectText = Object.values(state).filter(Boolean).join('\n\n')
      decision = decisionFromLabel(await $.model.classify(subjectText, TIERS))
    } catch (error) {
      log($, settings, `built-in classifier failed: ${String(error)}`)
    }
  }
  const ms = (await $.clock.now()) - startedAt
  return { decision, ms }
}

function remember(state: State, turnId: string, change: Change | null): void {
  state.applied.set(turnId, change)
  while (state.applied.size > APPLIED_CAPACITY) {
    const oldest = state.applied.keys().next().value
    if (oldest === undefined) break
    state.applied.delete(oldest)
  }
}

export const register: Register = (on, options) => {
  const settings = readSettings(options)
  const routeMain = settings.routeMainEffort || settings.mainModelRouting !== 'off'
  let state = freshState()

  on('session.start', ($, e, next) => {
    announce($, settings, state)
    const announced = state.announced
    state = freshState()
    state.announced = announced
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    announce($, settings, state)
    if (!routeMain) return next(e)
    const { decision, ms } = await classify($, settings, 'the turn', { prompt: e.text })
    log($, settings, `jev: ${describeDecision(decision, ms)}`)
    state.pending.put(e.text, decision)
    return next(e)
  })

  on('turn.start', ($, e, next) => {
    if (routeMain && e.text) {
      const decision = state.pending.take(e.text)
      if (decision !== undefined) state.byTurn.set(e.turnId, decision)
    }
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    if (!routeMain || e.agentId) return yield* next(e)

    // Every request after the first reuses what the turn settled on, so
    // neither the model nor the effort changes under a running tool loop.
    if (e.index > 0) {
      const change = state.applied.get(e.turnId)
      return yield* next(change ? { ...e, ...change } : e)
    }

    let decision = state.byTurn.get(e.turnId)
    state.byTurn.delete(e.turnId)
    if (decision === undefined) decision = state.pending.takeSole() ?? null

    // The main model may change only where no prompt cache exists yet: the
    // first prompt of a fresh session. A resumed session's first step is the
    // process's first too, but its transcript already has earlier prompts.
    let freshContext = false
    if (state.mainModelWindow && settings.mainModelRouting === 'session-start') {
      const turns = await $.session.turns()
      freshContext = turns <= 1
      $.ui.log(`first main step: turns ${turns}, messages ${e.messageCount}, model ${e.model}`, { to: 'debug' })
    }

    const routing = decide(decision, { model: e.model, effort: e.effort }, settings.policy)
    const change: Change = {}
    const suppressed: string[] = []
    if (routing.model) {
      if (settings.mainModelRouting === 'every-turn') {
        change.model = modelIdOf(routing.model)
      } else if (settings.mainModelRouting === 'off') {
        suppressed.push('main model routing off')
      } else if (!state.mainModelWindow) {
        suppressed.push('main model is picked at session start only')
      } else if (!freshContext) {
        suppressed.push('resumed session, main model kept for its prompt cache')
      } else {
        change.model = modelIdOf(routing.model)
      }
    }
    if (routing.effort) {
      if (settings.routeMainEffort) change.effort = routing.effort
      else suppressed.push('main effort routing off')
    }
    state.mainModelWindow = false

    const settled = Object.keys(change).length > 0 ? change : null
    remember(state, e.turnId, settled)
    if (settings.logDecisions) {
      $.ui.status(describeStatus(decision, settled))
      log($, settings, describeMainRouting(routing, settled, suppressed.length > 0 ? suppressed.join('; ') : null))
    }
    return yield* next(settled ? { ...e, ...settled } : e)
  })

  on('agent.spawn', async ($, e, next) => {
    announce($, settings, state)
    // A fork inherits its parent's context and model; `model` is ignored for it.
    if (!settings.routeSubagentModel || e.fork) return next(e)

    const { decision, ms } = await classify($, settings, 'the subagent', {
      prompt: e.prompt,
      description: e.description,
      agentType: e.subagentType,
    })
    log($, settings, `jev (${e.subagentType}): ${describeDecision(decision, ms)}`)

    // The caller's own model wins as the baseline; otherwise the subagent
    // would inherit the parent's. The Agent tool takes no effort, so only the
    // model is ours to set here, and an alias is fine: the tool takes one.
    const current = e.model ?? e.parentModel
    const routing = decide(decision, { model: current }, settings.policy)
    if (!routing.model) {
      log($, settings, `${e.subagentType}: ${routing.reason}`)
      return next(e)
    }
    log($, settings, `${e.subagentType} → ${routing.model}: ${routing.reason}`)
    return next({ ...e, model: routing.model })
  })
}
