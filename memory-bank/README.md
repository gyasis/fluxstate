# FluxState Memory Bank

This directory contains the **institutional memory** for the FluxState project. It provides complete context for resuming work across sessions, ensuring continuity regardless of individual memory constraints.

## Purpose

The Memory Bank serves as the single source of truth for:
- Project goals and business context
- Technical architecture and design decisions
- Current development status and priorities
- Known issues and their solutions
- User preferences and domain knowledge

## File Structure

The Memory Bank follows a hierarchical documentation pattern, where each file builds upon the previous:

### Core Documentation (Read in Order)

1. **projectbrief.md** - Foundation Document
   - What FluxState is and why it exists
   - Core concepts and key features
   - Target users and use cases
   - Current project status

2. **productContext.md** - Business Context
   - Why this project exists at Herself Health
   - Problems being solved
   - Strategic fit and success metrics
   - Target users and their needs

3. **systemPatterns.md** - Architecture & Design
   - Mirror table concept and implementation
   - Technology stack decisions
   - Design patterns and data flows
   - Critical implementation details

4. **techContext.md** - Technical Details
   - Dependencies and versions
   - Development environment setup
   - Testing strategy and coverage
   - Deployment considerations and constraints

5. **activeContext.md** - Current Work Focus
   - What's being worked on right now
   - Recent changes and updates
   - Active blockers and decisions needed
   - Next steps and priorities

6. **progress.md** - Implementation Status
   - What's complete, in-progress, and pending
   - Known issues and their severity
   - Release readiness checklist
   - Timeline estimates

### Supplementary Documentation

7. **CLAUDE.md** - Project Intelligence
   - Project-specific patterns and gotchas
   - User preferences and domain knowledge
   - Critical implementation paths
   - Quick reference commands

## How to Use This Memory Bank

### When Starting a New Session

1. **Quick Context** (5 minutes):
   - Read `activeContext.md` to understand current focus
   - Check `progress.md` for what's complete/pending
   - Review recent changes and blockers

2. **Deep Context** (15 minutes):
   - Read `projectbrief.md` for overall understanding
   - Read `systemPatterns.md` for architecture
   - Read `CLAUDE.md` for project-specific insights

3. **Full Context** (30 minutes):
   - Read all files in order (1-6)
   - Cross-reference with actual codebase
   - Validate that documentation matches reality

### When Resuming Work

Ask yourself these questions:
1. What was I working on last? → Check `activeContext.md`
2. What's already done? → Check `progress.md`
3. What decisions are pending? → Check `activeContext.md` (Critical Decisions section)
4. Are there any blockers? → Check `activeContext.md` (Blockers section)
5. What should I work on next? → Check `activeContext.md` (Next Steps section)

### When Making Changes

Always update the Memory Bank when:
- Completing a major feature or milestone
- Making architectural decisions
- Discovering project-specific patterns or gotchas
- Encountering and resolving issues
- Changing priorities or focus areas
- Making commits (update activeContext.md with what changed)

### Which File to Update

- **New feature complete?** → Update `progress.md`
- **Architecture changed?** → Update `systemPatterns.md`
- **Current focus shifted?** → Update `activeContext.md`
- **Discovered a pattern?** → Update `CLAUDE.md`
- **Business requirements changed?** → Update `productContext.md`
- **Dependencies changed?** → Update `techContext.md`
- **Project scope changed?** → Update `projectbrief.md`

## Maintenance Guidelines

### Keep Documentation Current
- Update Memory Bank in the same session as code changes
- Don't let documentation drift from reality
- Validate accuracy periodically (monthly recommended)

### Use Consistent Formatting
- UTF-8 symbols for status tracking:
  - :white_check_mark: Complete
  - :hourglass_flowing_sand: In progress
  - :white_large_square: Pending/not started
  - :x: Blocked/failed
  - :warning: Needs attention
  - :clock3: Waiting on external input

### Be Specific and Actionable
- Document *why* decisions were made, not just *what*
- Include concrete examples and code snippets
- Provide context for future maintainers
- Link to relevant files and line numbers

### Avoid Duplication
- Each piece of information should live in one place
- Cross-reference between files instead of copying
- Keep related information together

## Memory Bank Principles

1. **Completeness**: All context needed to resume work is captured
2. **Accuracy**: Documentation reflects current reality
3. **Accessibility**: Easy to find information quickly
4. **Actionability**: Clear next steps and priorities
5. **Continuity**: Enables seamless handoffs between sessions

## Version History

- **2026-02-09**: Initial Memory Bank creation
  - All core files established
  - Project at 85% completion (late development stage)
  - Focus: Production hardening and deployment prep

---

**Last Updated**: 2026-02-09

**Maintained By**: Memory Bank Keeper agent

**Review Schedule**: Update with every significant change, full review monthly
