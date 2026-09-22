# Setting up the guard skill for an agent

The skill is a text file the agent reads before every action, plus the `cli.js` it calls. Both
versions live here:

- `SKILL.md` — full: five checks (+ lead check), per-class thresholds, budget, timeout, log
- `SKILL-free.md` — two checks, no budget, no log

## Any agent

1. Copy the `jev/` folder into the agent's workspace (it has no dependencies; Node 22+).
2. Put `AI_GATEWAY_API_KEY` in `jev/.env`.
3. Put the skill file where the agent loads skills from, and make sure its system prompt says to
   read it before every action.
4. Set the budget: `--budget 100` on the CLI, or `budgetUsd` in `guard/actions.json` for the demo.
5. Prove it: `node jev/guard/run-day.js` and read `logs/guard-day.jsonl`.

## hermes / grok bot

Both load a skill as a markdown file with a `name` and `description` in the front matter, which is
what `SKILL.md` has. Put it in the directory the agent scans for skills (check the agent's own docs
for the exact path, it differs by version), and add one line to the agent's instructions:

> Before any action, run the jev-guard skill and obey its exit code.

The `--lite` flag on `cli.js` runs only the two free checks.
