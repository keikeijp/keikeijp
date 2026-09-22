---
name: jev-guard
description: Before every action, ask jev whether it is a duplicate, off goal, allowed to spend, reversible, and whether the last step worked. Run when jev is sure, hold for a person when it is not, stop on deny. Full version.
---

# jev guard (full)

Read this before **every single action**. No exceptions: not for "small" steps, not for retries.

## What you send

Call the guard with one JSON object on stdin:

```
echo '<json>' | node jev/guard/cli.js --budget 100 --log logs/guard.jsonl
```

```json
{
  "goal": "today's goal, one sentence",
  "action": {
    "type": "email | mail | order | pay | delete | render | build | search | log | ...",
    "description": "what you are about to do, in one plain sentence",
    "cost_usd": 0,
    "touches": ["who or what it affects: addresses, files, accounts"],
    "lead": { "thread": [ { "from": "...", "text": "..." } ] }
  },
  "history": [ { "type": "...", "description": "...", "cost_usd": 0, "status": "ok | failed | bounced | held | denied" } ],
  "spentTodayUsd": 12.4
}
```

- `history` is the last ten actions with their real status. jev can only judge what you show it: show it the history.
- When a lead is involved, `action.lead.thread` is the **full** message thread, not a summary.
- `spentTodayUsd` is what has actually been spent today. You count it. Nobody asks a model whether you've spent too much.

## What comes back

JSON with `outcome`, `reason`, the answers and their probabilities, and an exit code:

| exit | outcome | what you do |
|---|---|---|
| 0 | `run` | do the action, then append it to `history` with its real status |
| 2 | `hold` | do **not** do it. Tell the person what you wanted to do and why it was held, then wait |
| 3 | `deny` | do **not** do it, do not retry it, do not rephrase it. Tell the person |
| 1 | error | the guard itself failed. Treat as `hold` |

## The rules (these live in the code, not in your judgement)

Five questions on every action, a sixth when a lead is involved:

1. is this a duplicate of something already done
2. is this off goal
3. may it spend: approve / review / deny
4. can it be undone
5. did the last step actually work
6. is this lead real

- normal actions run when jev is at or above **0.8** on every check
- cheap actions (≤ $5) that jev says can be undone run at **0.7**
- mailing, ordering, paying, deleting, and anything jev is 90%+ sure cannot be undone, need **0.9**
- a **deny** always stops it, whatever the numbers say
- if jev doesn't answer within **3 seconds**, the action is held for a person
- the daily budget is a number in the code, counted, not asked; going over it holds the action before jev is even called

## After a hold

Do not try to get around it. Do not split the action into smaller pieces, reword it, or lower the cost so it passes. A hold means a person decides. Move on to the next independent action if there is one, otherwise wait.

## Logging

Every decision is appended to the log file as one JSON line: the action, the answers, the probabilities, the threshold used and what happened. Do not edit the log. Point the person at it when they ask what you did.
