#!/bin/bash
# KAVACH-ULTRA 2026 — GitHub Sync Script
# Pushes all VPS-fixed files to GitHub
# Run from: /home/ubuntu/KAVACH-ULTRA-2026/
# Usage: bash sync_to_github.sh "your commit message"

set -e   # Exit on any error

COMMIT_MSG="${1:-Fix: Apply all audit fixes (confidence, divergence, blackswan, cooldown)}"
BRANCH="main"

echo "🛡 KAVACH-ULTRA GitHub Sync"
echo "================================"

# ── Check git status ──────────────────────────────────────────────────────
echo "📋 Changed files:"
git status --short

echo ""
echo "📦 Staging all changes..."
git add -A

# ── Check if there's anything to commit ──────────────────────────────────
if git diff --cached --quiet; then
    echo "✅ Nothing to commit — already up to date."
    exit 0
fi

echo "✍️  Committing: '$COMMIT_MSG'"
git commit -m "$COMMIT_MSG"

echo "🚀 Pushing to GitHub ($BRANCH)..."
git push origin "$BRANCH"

echo ""
echo "✅ Done! All fixes are now on GitHub."
echo ""
echo "Files pushed:"
git log -1 --name-status --format=""
