# Antigravity Kit Architecture

> Comprehensive AI Agent Capability Expansion Toolkit

---

## 📋 Overview

Antigravity Kit is a modular system consisting of:

- **25 Specialist Agents** - Role-based AI personas
- **17 Skills** - Workflow, advisory & project-management knowledge modules (includes oracle & librarian)
- **32 Workflows** - Slash command procedures
- **4 Scripts** - Master validation & session scripts
- **1 Rule Set** - Global rules (`rules/GEMINI.md`)

---

## 🏗️ Directory Structure

```plaintext
.agents/
├── ARCHITECTURE.md          # This file
├── agents/                  # 25 Specialist Agents
├── skills/                  # 17 Skills
├── workflows/               # 32 Slash Commands
├── rules/                   # Global Rules (GEMINI.md)
├── scripts/                 # Master Validation & Session Scripts (4)
└── .shared/                 # Shared assets (ui-ux-pro-max data + search scripts)
```

---

## 🤖 Agents (25)

Specialist AI personas for different domains.

All agents (except `multimodal-analyst`) also declare the legacy `clean-code` skill.

| Agent                    | Focus                      | Skills Declared                                           |
| ------------------------ | -------------------------- | --------------------------------------------------------- |
| `orchestrator`           | Multi-agent coordination   | parallel-agents, behavioral-modes, plan-writing, brainstorming, architecture |
| `project-planner`        | Discovery, task planning   | app-builder, plan-writing, brainstorming                  |
| `frontend-specialist`    | Web UI/UX                  | nextjs-react-expert, web-design-guidelines, tailwind-patterns, frontend-design, lint-and-validate |
| `backend-specialist`     | API, business logic        | nodejs-best-practices, python-patterns, api-patterns, database-design, mcp-builder |
| `database-architect`     | Schema, SQL                | database-design                                           |
| `mobile-developer`       | iOS, Android, RN           | mobile-design                                             |
| `game-developer`         | Game logic, mechanics      | game-development (+11 platform subskills)                 |
| `devops-engineer`        | CI/CD, Docker              | deployment-procedures, server-management                  |
| `security-auditor`       | Security compliance        | vulnerability-scanner, red-team-tactics, api-patterns     |
| `penetration-tester`     | Offensive security         | vulnerability-scanner, red-team-tactics, api-patterns     |
| `test-engineer`          | Testing strategies         | testing-patterns, tdd-workflow, webapp-testing, code-review-checklist, lint-and-validate |
| `debugger`               | Root cause analysis        | systematic-debugging                                      |
| `performance-optimizer`  | Speed, Web Vitals          | performance-profiling                                     |
| `seo-specialist`         | Ranking, visibility        | seo-fundamentals, geo-fundamentals                        |
| `documentation-writer`   | Manuals, docs              | documentation-templates                                   |
| `product-manager`        | Requirements, user stories | plan-writing, brainstorming                               |
| `product-owner`          | Strategy, backlog, MVP     | plan-writing, brainstorming                               |
| `qa-automation-engineer` | E2E testing, CI pipelines  | webapp-testing, testing-patterns, web-design-guidelines, lint-and-validate |
| `code-archaeologist`     | Legacy code, refactoring   | refactoring-patterns, code-review-checklist               |
| `explorer-agent`         | Codebase analysis          | architecture, plan-writing, brainstorming, systematic-debugging |
| `oracle`                 | Strategic technical advisor| oracle, architecture, systematic-debugging, code-review-checklist |
| `librarian`              | OSS docs & research        | librarian, documentation-templates                        |
| `plan-consultant`        | Pre-planning analysis      | brainstorming, architecture                               |
| `plan-reviewer`          | Plan validation            | plan-writing, code-review-checklist                       |
| `multimodal-analyst`     | Visual content analysis    | —                                                         |

> Only the skills listed in the Skills section below are bundled in `skills/`.
> Other declared names are legacy references from the agent frontmatter — the
> guidance for those domains is embedded in each agent's definition file.

---

## 🧩 Skills (17)

Modular knowledge domains that agents can load on-demand based on task context.

### Workflow & Process

| Skill                             | Description                                                                 |
| --------------------------------- | --------------------------------------------------------------------------- |
| `using-superpowers`               | Entry point — how to discover and invoke skills at conversation start       |
| `brainstorming`                   | Explore intent & requirements before creative/implementation work           |
| `writing-plans`                   | Turn specs/requirements into multi-step implementation plans                |
| `executing-plans`                 | Execute a written plan in a separate session with review checkpoints        |
| `dispatching-parallel-agents`     | Fan out 2+ independent tasks with no shared state                           |
| `subagent-driven-development`     | Implementer + reviewer subagents per plan task                              |
| `using-git-worktrees`             | Isolate feature work in a dedicated workspace                               |
| `finishing-a-development-branch`  | Structured merge/PR/cleanup decision after completed work                   |
| `test-driven-development`         | Write the failing test before implementation code                           |
| `systematic-debugging`            | Root-cause tracing for bugs/test failures before proposing fixes            |
| `verification-before-completion`  | Run verification commands; evidence before completion claims                |
| `requesting-code-review`          | Dispatch a reviewer subagent with precisely crafted context                 |
| `receiving-code-review`           | Verify review feedback technically before implementing it                   |
| `writing-skills`                  | Create, edit, and verify skills before deployment                           |

### Project Management

| Skill       | Description                                                                     |
| ----------- | ------------------------------------------------------------------------------- |
| `conductor` | Context-driven development: spec-first tracks, phases/tasks, beads integration, 16 slash commands |

### Advisory & Research

| Skill       | Description                                                                     |
| ----------- | ------------------------------------------------------------------------------- |
| `oracle`    | Read-only strategic technical advisor for architecture, debugging, code review  |
| `librarian` | Open-source research: documentation lookup, multi-repo analysis, usage examples |

### Skills with Assets

| Skill                           | Files | Contents                                                    |
| ------------------------------- | ----- | ----------------------------------------------------------- |
| `conductor`                     | 22    | 6 references (incl. beads integration) + 16 commands        |
| `systematic-debugging`          | 8     | Debugging references + creation log                         |
| `using-superpowers`             | 5     | Tool-specific references (antigravity, gemini, hermes, pi, codex) |
| `writing-skills`                | 4     | Best practices, persuasion, subagent testing + examples     |
| `subagent-driven-development`   | 3     | Implementer / task-reviewer / re-review prompts             |
| `brainstorming`                 | 2     | Spec-document reviewer, visual companion                    |
| `writing-plans`                 | 1     | Plan-document reviewer prompt                               |
| `requesting-code-review`        | 1     | Code-reviewer prompt                                        |
| `test-driven-development`       | 1     | Writing-good-tests reference                                |

---

## 🔄 Workflows (32)

Slash command procedures. Invoke with `/command`.

### Core Workflows

| Command          | Description                         |
| ---------------- | ----------------------------------- |
| `/brainstorm`    | Socratic discovery                  |
| `/create`        | Create new features                 |
| `/debug`         | Debug issues                        |
| `/deploy`        | Deploy application                  |
| `/enhance`       | Improve existing code               |
| `/orchestrate`   | Multi-agent coordination            |
| `/plan`          | Task breakdown                      |
| `/preview`       | Preview changes                     |
| `/status`        | Check project status                |
| `/test`          | Run tests                           |
| `/ui-ux-pro-max` | Design intelligence: 50+ styles, 97 palettes, searchable database |
| `/init-deep`     | Deep context initialization         |
| `/ralph-loop`    | Continuous dev loop until completion|
| `/start-work`    | Execute from planner-generated plan |
| `/refactor`      | Intelligent refactoring with TDD    |
| `/handoff`       | Session context transfer            |

### Conductor Workflows

| Command                | Description                              |
| ---------------------- | ---------------------------------------- |
| `/conductor-setup`     | Initialize project with Conductor        |
| `/conductor-newtrack`  | Create new feature/bug track with spec   |
| `/conductor-implement` | Execute tasks from track plan            |
| `/conductor-status`    | Display project progress                 |
| `/conductor-revert`    | Git-aware revert of tracks/phases/tasks  |
| `/conductor-validate`  | Validate project integrity               |
| `/conductor-block`     | Mark task as blocked with reason         |
| `/conductor-skip`      | Skip current task with reason            |
| `/conductor-revise`    | Update spec/plan for implementation issues|
| `/conductor-archive`   | Archive completed tracks                 |
| `/conductor-export`    | Export project summary as markdown       |
| `/conductor-handoff`   | Create context handoff for next session  |
| `/conductor-refresh`   | Sync context docs with codebase state    |
| `/conductor-formula`   | Manage track workflow templates          |
| `/conductor-distill`   | Extract reusable template from track     |
| `/conductor-wisp`      | Quick ephemeral exploration track        |

---

## 🎯 Skill Loading Protocol

```plaintext
User Request → Skill Description Match → Load SKILL.md
                                            ↓
                                    Read references/
                                            ↓
                                    Read scripts/
```

### Skill Structure

```plaintext
skill-name/
├── SKILL.md           # (Required) Metadata & instructions
├── scripts/           # (Optional) Python/Bash scripts
├── references/        # (Optional) Templates, docs
└── assets/            # (Optional) Images, logos
```

---

## Scripts (4)

Master validation and session-management scripts.

| Script              | Purpose                                                                 |
| ------------------- | ----------------------------------------------------------------------- |
| `checklist.py`      | Priority-based validation (P0 security → P6 performance)                |
| `verify_all.py`     | Full pre-deployment suite: checklist + Lighthouse, Playwright E2E, bundle, mobile |
| `auto_preview.py`   | Dev server lifecycle (start/stop/status) for previewing the app         |
| `session_manager.py`| Project state analysis, tech-stack detection, session summary           |

### Usage

```bash
# Quick validation during development
python .agents/scripts/checklist.py . [--url URL]

# Full verification before deployment
python .agents/scripts/verify_all.py . --url http://localhost:3000

# Manage the preview dev server
python .agents/scripts/auto_preview.py start|stop|status [port]

# Analyze project state / session summary
python .agents/scripts/session_manager.py status|info [path]
```

### What They Check

**checklist.py** (Core checks):

- Security (vulnerabilities, secrets)
- Code Quality (lint, types)
- Schema Validation
- Test Suite
- UX Audit
- SEO Check

**verify_all.py** (Full suite):

- Everything in checklist.py PLUS:
- Lighthouse (Core Web Vitals)
- Playwright E2E
- Bundle Analysis
- Mobile Audit

**Shared assets**: `.shared/ui-ux-pro-max/` holds 29 files — design database
CSVs (styles, palettes, typography, UX guidelines, 9 tech stacks) and Python
search scripts backing the `/ui-ux-pro-max` workflow.

---

## 📊 Statistics

| Metric              | Value                         |
| ------------------- | ----------------------------- |
| **Total Agents**    | 25                            |
| **Total Skills**    | 17                            |
| **Total Workflows** | 32                            |
| **Total Scripts**   | 4                             |
| **Rules**           | 1 (GEMINI.md)                 |
| **Shared Assets**   | 29 files under `.shared/ui-ux-pro-max/` |
| **Coverage**        | Roles span planning, design, development, testing, security, operations, review |

---

## 🔗 Quick Reference

| Need           | Agent                 | Skills                                |
| -------------- | --------------------- | ------------------------------------- |
| Plan           | `project-planner`     | brainstorming, plan-writing†          |
| Pre-Analysis   | `plan-consultant`     | brainstorming, architecture†          |
| Plan Review    | `plan-reviewer`       | plan-writing†, code-review-checklist† |
| Architecture   | `oracle`              | oracle, systematic-debugging          |
| Research       | `librarian`           | librarian                             |
| Debug          | `debugger`            | systematic-debugging                  |
| Testing        | `test-engineer`       | testing-patterns†, webapp-testing†    |
| Code Review    | `plan-reviewer`       | requesting-code-review, receiving-code-review |
| Parallel Work  | `orchestrator`        | dispatching-parallel-agents, subagent-driven-development |
| Feature Branch | (any agent)           | using-git-worktrees, finishing-a-development-branch |
| Completion     | (any agent)           | verification-before-completion        |
| Web App        | `frontend-specialist` | web-design-guidelines†, frontend-design† |
| API            | `backend-specialist`  | api-patterns†, nodejs-best-practices† |
| Database       | `database-architect`  | database-design†                      |
| Security       | `security-auditor`    | vulnerability-scanner†                |
| Mobile         | `mobile-developer`    | mobile-design†                        |
| Visual Content | `multimodal-analyst`  | —                                     |

† Declared in agent frontmatter but no longer bundled in `skills/` — guidance is embedded in the agent definition.
