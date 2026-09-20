/**
 * report.ts — the lines the router writes about its own work.
 *
 * Nothing else in Claude Code shows a rewritten `model` or `effort`: they are
 * parameters of one request, not the session's settings, so the header and
 * the effort box never move. These lines are the only evidence the router
 * ran, and what it did.
 */
import type { Backend, Decision } from './jev.ts'
import { effortOfScore } from './policy.ts'
import type { Effort, Routing } from './policy.ts'

export const TAG = '[jev-model-router]'

export interface Switches {
  subagentModel: boolean
  mainEffort: boolean
  mainModel: 'session-start' | 'every-turn' | 'off'
}

function num(value: number | null): string {
  return value === null ? 'n/a' : value.toFixed(2)
}

/** Said once per session: which backend answers, and which switches are on. */
export function describeSetup(backend: Backend | null, switches: Switches, builtinByChoice: boolean): string {
  const where = backend
    ? `${backend.provider} (${backend.url})`
    : builtinByChoice
      ? 'the built-in classifier, by choice'
      : 'the built-in classifier, no key set'
  const on = [
    switches.subagentModel && 'subagent model',
    switches.mainEffort && 'main effort',
    switches.mainModel !== 'off' && `main model (${switches.mainModel})`,
  ].filter(Boolean)
  return `ready on ${where}; routing ${on.length > 0 ? on.join(', ') : 'nothing, every switch is off'}`
}

/** What Jev answered, raw, and how long it took. */
export function describeDecision(decision: Decision | null, ms: number | null): string {
  const took = ms === null ? '' : ` · ${Math.round(ms)}ms`
  if (!decision) return `no answer${took}`
  const parts = [`tier ${decision.tier} (${num(decision.tierConfidence)})`]
  if (decision.effort !== null) {
    parts.push(
      `effort ${decision.effort.toFixed(1)} → ${effortOfScore(decision.effort)} (${num(decision.effortConfidence)})`,
    )
  }
  if (decision.risk !== null) parts.push(`risky ${num(decision.risk)}`)
  return parts.join(' · ') + took
}

export interface Change {
  model?: string
  effort?: Effort
}

/** What the policy did with the answer on the main loop. */
export function describeMainRouting(routing: Routing, change: Change | null, suppressed: string | null): string {
  if (!change) {
    if (!suppressed) return `main loop: ${routing.reason}`
    const wanted = [routing.model, routing.effort && `effort ${routing.effort}`].filter(Boolean).join(', ')
    return `main loop kept as is, wanted ${wanted}: ${routing.reason} (${suppressed})`
  }
  const what = [change.model, change.effort && `effort ${change.effort}`].filter(Boolean).join(', ')
  return `main loop → ${what}: ${routing.reason}${suppressed ? ` (${suppressed})` : ''}`
}

/** The one-line status pinned under the prompt, replaced as it goes. */
export function describeStatus(decision: Decision | null, change: Change | null): string {
  if (!decision) return 'jev · no answer'
  const asked = `${decision.tier} ${num(decision.tierConfidence)}`
  if (!change) return `jev · ${asked} · unchanged`
  const to = [change.model, change.effort].filter(Boolean).join('/')
  return `jev · ${asked} → ${to}`
}
