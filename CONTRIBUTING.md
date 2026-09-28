# Working on DriftWatch without conflicts

Read `docs/DriftWatch_Project_Plan.docx` (the plan) and `docs/interfaces.md` (the module contracts) first.

## The 6 rules that prevent merge pain
1. **One owner per file.** Ownership is in `.github/CODEOWNERS` and in every issue ("Files you own"). If you need a change in someone else's file, comment on their issue or ask in the group; do not edit it.
2. **One issue = one branch = one PR.** Branch name is in the issue (e.g. `feature/tailer`). Branch from latest `main`.
3. **Small PRs, merge often.** Open a PR as soon as the acceptance criteria are met, ideally within a day. Long-lived branches are where drift comes from.
4. **Sync before every session:** `git fetch origin && git rebase origin/main` (or `git pull --rebase`). Resolve conflicts locally, never in the GitHub web editor.
5. **Contracts are frozen.** `docs/interfaces.md`, `backend/app/alerts/schema.py` and `docs/alert-schema.md` only change by a PR approved by all four people, announced in the group first.
6. **Merge through PRs only, reviewed by one other person.** Squash-merge. Never push to `main`. Never `git push --force` on a shared branch.

## Shared files
| File | Rule |
|---|---|
| `backend/requirements.txt` | Pre-populated. Need a dependency? Tiny PR touching only this file. |
| `frontend/package.json` | Ayush creates it in the Vite scaffold issue. Later additions: tiny PR touching only this file (+ lockfile). |
| `.env.example` | Add your own variables at the bottom in a separate block; tiny PR. |
| `docs/interfaces.md` | See rule 5. |

## No mock data
Nothing hardcoded, nothing faked. The frontend develops against the real running backend, the engine against the real generator log. If a piece you depend on is not merged yet, write unit tests with tiny inline sample inputs, and integrate when the dependency lands (issues list their dependencies).

## Local ports
Backend API + WebSocket `:8000`, generator control API `:8001`, Vite dev server `:5173`.

## Secrets
Never commit keys or tokens. Use `.env` (git-ignored) and update `.env.example` when you add a setting.

## Commit messages
`area: short imperative summary` e.g. `ingest: handle log rotation by inode`.
