---
description: Self-referential development loop that runs continuously until task completion. Auto-continues on premature stops.
---

# /ralph-loop — Continuous Development Loop

A self-referential development loop that drives tasks to completion without manual intervention.

## Steps

1. **Accept the task goal**
   - User provides: `/ralph-loop "Build a REST API with authentication"` or similar
   - Parse the goal and optional max iterations (default: 50)

2. **Create initial plan**
   - Break the goal into concrete todo items
   - Estimate complexity and ordering
   - Set iteration counter to 0

3. **Execute loop iteration**
   - Pick the next incomplete todo item
   - Work on it using appropriate tools and agents
   - Mark completed items done
   - Increment iteration counter

4. **Check completion after each iteration**
   - Are ALL todo items marked complete?
   - Has the original goal been fully addressed?
   - Do tests/builds pass?
   - If YES to all → Exit loop with completion report
   - If NO → Continue to next iteration

5. **Handle premature stops**
   - If the agent stops without completion, auto-continue
   - Assess what was done and what remains
   - Resume from where it left off

6. **Exit conditions** (any of these terminates the loop)
   - ✅ All todos completed and verified
   - 🔢 Max iterations reached (report what's done vs remaining)
   - 🛑 User cancels with `/cancel-ralph` or equivalent stop signal
   - ❌ Unrecoverable error (3+ consecutive failures on same task)

7. **Completion report**
   ```
   ## Ralph Loop Complete
   - **Goal**: [original goal]
   - **Iterations**: N
   - **Status**: [Complete | Partial | Cancelled]
   - **Completed**: [list of done items]
   - **Remaining**: [list of undone items, if any]
   - **Notes**: [any important observations]
   ```

## Options

- `--max-iterations=N`: Maximum loop iterations (default: 50)
- `--verbose`: Report progress after each iteration

## Rules

- Never work on the same failed task more than 3 times
- Always verify work before marking complete
- Create detailed todos BEFORE starting the loop
- Track progress obsessively
- If stuck on a task, try a different approach before escalating
