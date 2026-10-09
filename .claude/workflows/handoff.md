---
description: Create a detailed context summary for continuing work in a new session. Captures state, completed work, remaining tasks, and relevant file paths.
---

# /handoff — Session Context Transfer

Generate a structured handoff document that enables seamless continuation in a new session.

## Steps

1. **Gather current context**
   - What was the original goal/task?
   - What has been completed so far?
   - What remains to be done?
   - What were the key decisions made?

2. **Document completed work**
   - List all files created or modified
   - Summarize each change and its purpose
   - Note any important patterns established
   - Record test results and build status

3. **Document remaining work**
   - List pending tasks with priority
   - Note any blockers or open questions
   - Identify dependencies between remaining tasks
   - Flag any issues that need user decision

4. **Capture technical context**
   - Current branch and recent commits
   - Relevant file paths and line numbers
   - Key function/class names involved
   - Configuration changes made
   - Environment setup notes

5. **Generate handoff document**
   Create a markdown file with this structure:
   ```markdown
   # Session Handoff: [Task Title]
   
   **Date**: [current date]
   **Branch**: [git branch]
   **Status**: [In Progress / Blocked / Nearly Complete]
   
   ## Original Goal
   [What the user asked for]
   
   ## Completed Work
   - [x] [Task 1]: [brief description] → `path/to/file`
   - [x] [Task 2]: [brief description] → `path/to/file`
   
   ## Remaining Work
   - [ ] [Task 3]: [brief description]
     - Depends on: [dependencies]
     - Notes: [important context]
   - [ ] [Task 4]: [brief description]
   
   ## Key Decisions
   - [Decision 1]: [rationale]
   - [Decision 2]: [rationale]
   
   ## Important Files
   - `path/to/file1` — [role/purpose]
   - `path/to/file2` — [role/purpose]
   
   ## Open Questions
   - [Question that needs user input]
   
   ## To Resume
   1. Read this handoff document
   2. Check out branch: `[branch name]`
   3. Start with: [specific next action]
   ```

6. **Save the handoff**
   - Save to project root or `.agent/` directory
   - Name format: `handoff-[task-slug]-[date].md`
   - Report the file path to the user

## Notes

- Handoff documents should be self-contained
- Include enough context that a different agent/session can continue
- Be specific about file paths — no vague references
- Always include "To Resume" instructions
