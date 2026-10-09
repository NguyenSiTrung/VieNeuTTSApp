---
description: Start executing a previously created plan. Distributes tasks to specialized agents, accumulates learnings, and verifies completion.
---

# /start-work — Plan Execution Workflow

Execute tasks from a planner-generated plan file, distributing work to specialized agents.

## Steps

1. **Find the plan file**
   - Look for recent plan files (`.md` files created by `/plan` or `project-planner`)
   - Common locations: project root, `.agent/` directory, artifacts
   - If multiple plans exist, ask user which one to execute
   - If no plan exists, suggest running `/plan` first

2. **Parse the plan**
   - Extract all task items from the plan
   - Identify dependencies between tasks
   - Determine execution order (dependency-aware)
   - Note required skills and agents for each task

3. **Pre-flight validation**
   - Optionally invoke `plan-reviewer` to validate the plan
   - Verify referenced files exist
   - Check that required tools are available
   - If blockers found → report and ask user how to proceed

4. **Execute tasks systematically**
   For each task in dependency order:
   
   a. **Select the right agent/skill**
      - Match task domain to specialist agent
      - Load relevant skills
   
   b. **Delegate with full context**
      Follow the delegation-patterns protocol:
      ```
      1. TASK: [from plan]
      2. EXPECTED OUTCOME: [from plan]
      3. REQUIRED TOOLS: [matched to task]
      4. MUST DO: [from plan + accumulated learnings]
      5. MUST NOT DO: [from plan]
      6. CONTEXT: [file paths, patterns, previous results]
      ```
   
   c. **Verify result**
      - Check success criteria from plan
      - Run tests/diagnostics on changed files
      - If failed → retry with error context (max 3 attempts)
   
   d. **Accumulate learnings**
      - Record what worked and what didn't
      - Pass learnings to subsequent tasks
      - Note discovered patterns and conventions

5. **Handle parallel tasks**
   - Identify tasks with no dependencies on each other
   - Execute independent tasks in parallel
   - Collect results before starting dependent tasks

6. **Completion verification**
   - All tasks marked complete?
   - Build passes?
   - Tests pass?
   - Plan's acceptance criteria met?

7. **Final report**
   ```
   ## Work Session Complete
   - **Plan**: [plan name]
   - **Tasks completed**: N/M
   - **Key findings**: [learnings accumulated]
   - **Remaining**: [if any tasks weren't completed]
   - **Verification**: [build/test status]
   ```

## Notes

- Tasks are executed in dependency order
- Learnings from early tasks inform later tasks
- Failed tasks are retried up to 3 times before skipping
- Always verify after completing each task
