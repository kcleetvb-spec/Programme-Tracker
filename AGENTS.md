# AGENTS.md

## Standing rules (user mandates — apply to every change)
1. 每次改動完成後，都必須創建一個對應的 Git commit，以便後續追蹤和回滾。
   Commit after every change, so it can be tracked and rolled back.
2. 每次改動後，都必須編寫或更新相關測試，並在交付給用戶前，確保所有測試和驗證全部通過。
   Write or update related tests after every change; all tests and verifications must pass before delivery. (This repo currently has no test suite — rule applies once code/tests exist.)

## Repo facts
- Template/scaffold repo ("VC project template") holding per-tool AI coding configs — not an application. There are no build, lint, typecheck, or test commands; verification = `git status` clean + the rules above.
- Git root is this directory (`/Users/kclee/Documents/Project/Coding`). `AiStudio/`, `Cursor/`, `Opencode/`, `Trae/` are per-tool config homes (mostly empty placeholders), not separate repos.
- Local-only repo: no git remote. Push is not expected.
- Commit messages follow Conventional Commits (`docs:`, `chore:`, …) — see `git log`.
- `.gitignore` excludes `.codegraph` (a symlink to ~/.omo/codegraph) and `.omo/` (session artifacts) — never commit them. `.DS_Store` files are untracked noise; do not commit.
- This file was previously named `Agents.md`; keep the canonical name `AGENTS.md` (renamed so case-sensitive systems pick it up).
