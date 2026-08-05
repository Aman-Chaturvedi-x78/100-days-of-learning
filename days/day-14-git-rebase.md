# Day 14 — Git: Interactive Rebase & Branch Management

Date: Day 14

## Goal
Learn how to use git branches effectively and perform interactive rebases to clean up commit history before merging.

## Why this matters
A clean, logical commit history makes code review easier and helps future-you (and teammates) understand why changes were made. Interactive rebase lets you reorder, squash, edit, or drop commits before they reach a shared branch.

## Common commands
- git checkout -b feature/short-description  # create & switch to a feature branch
- git fetch origin && git checkout main && git pull  # update local main
- git rebase main  # move current branch on top of latest main
- git merge --no-ff feature/xyz  # merge keeping a merge commit

## Interactive rebase basics
Start an interactive rebase to rewrite the last N commits:

```bash
# Start an interactive rebase for the last 4 commits
git rebase -i HEAD~4
```

An editor opens showing lines like:

pick a1b2c3d Add initial implementation
pick b2c3d4e Fix bug in handler
pick c3d4e5f Improve tests
pick d4e5f6g Small refactor

Change the action keywords to:
- pick — keep
- reword — change commit message
- edit — pause to amend the commit
- squash — combine into previous commit, keep both messages
- fixup — combine into previous commit, discard this commit's message
- drop — remove commit

Example: squash the last two commits into the previous one:

```text
pick a1b2c3d Add initial implementation
pick b2c3d4e Fix bug in handler
squash c3d4e5f Improve tests
squash d4e5f6g Small refactor
```

Save and close the editor; another editor opens to let you edit the combined commit message.

## Rebase conflict resolution
If a conflict happens during rebase:

```bash
# Resolve the conflicting files in your editor, then:
git add <file>
# Continue the rebase
git rebase --continue
# If you want to abort the rebase and return to original branch state:
git rebase --abort
```

If you used `edit` to amend a commit:

```bash
# make changes
git add <file>
git commit --amend --no-edit  # or change message
git rebase --continue
```

## Rebase vs Merge (short)
- Rebase: rewrites history to create a linear history. Cleaner but rewrites commits (avoid on published branches).
- Merge: preserves history and creates a merge commit. Safer for shared branches.

Best practice: rebase local feature branches onto updated main to keep history linear, but never rebase commits that have been pushed and shared with others unless everyone agrees.

## Example workflow

```bash
# Create a branch
git checkout -b feature/awesome
# Work and commit
# Update main
git fetch origin
git checkout main
git pull
# Rebase your branch on top of main
git checkout feature/awesome
git rebase main
# Resolve conflicts if any
# Squash / reorder commits interactively before opening PR
git rebase -i main
# Push (force-push required after rewriting history)
git push --force-with-lease origin feature/awesome
```

Note: use --force-with-lease instead of --force when pushing rewritten history to avoid clobbering others' work.

## Tips
- Use `git log --oneline --graph --decorate` to preview branch shape.
- Use `git reflog` to recover commits if a rebase went wrong.
- Keep commits small and focused; commit messages should explain why, not just what.

## References
- Pro Git book — Rewriting history: https://git-scm.com/book/en/v2/Git-Tools-Rewriting-History
- Git docs — git-rebase: https://git-scm.com/docs/git-rebase

---

Short demo code snippets and commands are above — try them in a throwaway branch first.
