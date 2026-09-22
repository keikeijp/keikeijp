---
name: jev-guard-lite
description: Before every action, ask jev whether it is a duplicate of something already done and whether it is off goal. Hold for a person when jev is not sure. Free version, two checks.
---

# jev guard (free)

Read this before **every single action**.

Two checks, one jev call:

1. is this a duplicate of something already done
2. is this off goal

Send jev the action, today's goal and the last ten actions with their status. Run when jev is at or
above **0.8** sure it is *not* a duplicate and *not* off goal. Otherwise hold it and tell the person
what you wanted to do and why it was held. Do not reword the action to get it through.

```
echo '{"goal":"...","action":{"type":"...","description":"...","cost_usd":0},"history":[...]}' \
  | node jev/guard/cli.js --lite
```

The full version adds: may it spend (approve / review / deny, a deny always stops it), can it be
undone, did the last step actually work, is this lead real, per-class thresholds (0.7 / 0.8 / 0.9),
a counted daily budget, the 3 second timeout hold, and the JSON log of every decision.
