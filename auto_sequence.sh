#!/bin/zsh
# Run by launchd (~/Library/LaunchAgents/in.onecourt.sc-sequence.plist) every
# 10 minutes in the morning, Mon–Sat: save new hearing sequences and publish.
export PATH=/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin
cd /Users/mohitsingh/SLP/sc/causelist || exit 1
echo "── $(date '+%Y-%m-%d %H:%M')"
/usr/bin/python3 fetch_sequence.py 2>/dev/null
git add data/seq-*.json
if ! git diff --cached --quiet; then
  git commit -qm "Update hearing sequences ($(date '+%Y-%m-%d %H:%M IST'))" &&
  git pull -q --rebase && git push -q && echo "published"
fi
