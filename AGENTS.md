# AGENTS.md

## Standing rules (apply to every change)
1. **Commit after every change.** Create a Git commit after completing each change so it can be tracked and rolled back.
2. **Update/add tests when they exist.** After each change, write or update related tests. Before delivery, all tests and verifications must pass. (No test suite exists yet; apply this rule once code/tests appear.)

## Repo facts
- **Template/scaffold only.** Holds per-tool AI coding configs (`AiStudio/`, `Cursor/`, `Opencode/`, `Trae/`) — not an application.
- **No runnable project tooling.** No build, lint, typecheck, test, or codegen commands defined here. Verification = `git status` clean (plus tests once they exist).
- **Single git root.** Git root is `/Users/kclee/Documents/Project/Coding`. The per-tool directories are config homes (not separate repos).
- **Local-only.** No git remote; pushing is not expected.
- **Commit style.** Conventional Commits (`docs:`, `chore:`, etc.) — follow existing history in `git log`.
- **Never commit excluded artifacts.** `.gitignore` excludes `.codegraph` (symlink to `~/.omo/codegraph`) and `.omo/` (session artifacts). Do not commit `.DS_Store`.
- **Canonical filename.** Keep this file as `AGENTS.md` (case-sensitive systems expect this; it was renamed from `Agents.md`).
