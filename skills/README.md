# FluxState skills (flat, for Claude Code)

Single-file Claude Code skills so any agent session can *use* FluxState. Each is a flat `.md` with
YAML frontmatter (`name` + `description`); the `description` is the trigger the harness matches on.

| Skill | Use for |
|---|---|
| [`fluxstate.md`](fluxstate.md) | Overview + router — the mental model, the `.flux` format, which skill to pick |
| [`fluxstate-capture.md`](fluxstate-capture.md) | **Write** — capture/append snapshots into a store (idempotent; deletes) |
| [`fluxstate-timetravel.md`](fluxstate-timetravel.md) | **Read** — as-of reconstruction, per-cell timeline, row lifecycle, `info`, DuckDB glob |
| [`fluxstate-compare.md`](fluxstate-compare.md) | **Compare** — A/B & change-over-time diff + launch the Temporal Viewer |

## Install (copy into your skills dir)

Claude Code discovers flat skills in `~/.claude/skills/`:

```bash
# from the repo root
cp skills/fluxstate*.md ~/.claude/skills/
```

(Or symlink them so they track the repo: `for f in skills/fluxstate*.md; do ln -sf "$PWD/$f" ~/.claude/skills/; done`.)

Then in a Claude Code session, `/fluxstate` (or a natural-language trigger from a skill's
`description`) invokes it. Deep reference: [`../AGENTS.md`](../AGENTS.md) and [`../docs/API.md`](../docs/API.md).
