# Quick Reference: Discard 6 Commits Ahead of Upstream

This is a quick reference for discarding commits ahead of the upstream fork.

## Quick Command Sequence

For the `chorty/extended_openai_conversation` fork (upstream is `jekalmin/extended_openai_conversation`):

```bash
# Step 1: Add upstream remote (one-time setup)
git remote add upstream https://github.com/jekalmin/extended_openai_conversation.git

# Step 2: Fetch upstream changes
git fetch upstream

# Step 3: Switch to your branch (e.g., develop)
git checkout develop

# Step 4: OPTIONAL - View commits to be discarded
git log upstream/develop..HEAD --oneline

# Step 5: Reset to upstream (discards ALL commits ahead of upstream)
git reset --hard upstream/develop

# Step 6: Force push to your fork
git push origin develop --force
```

## Alternative: Discard Only the Last 6 Commits

If you want to keep earlier commits but discard only the last 6:

```bash
# Go back 6 commits
git reset --hard HEAD~6

# Force push
git push origin develop --force
```

## Important Notes

- ⚠️ **WARNING**: These commands permanently discard commits
- 💾 **Backup first**: Create a backup branch before resetting:
  ```bash
  git branch backup-before-reset
  ```
- 🔍 **Verify**: Check what you're discarding with `git log upstream/develop..HEAD`
- 👥 **Coordinate**: If others are working on this branch, coordinate before force pushing

## See Full Guide

For detailed explanation, safety tips, and recovery options, see [DISCARD_COMMITS_GUIDE.md](./DISCARD_COMMITS_GUIDE.md).
