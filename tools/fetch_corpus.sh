#!/usr/bin/env bash
# Κατεβάζει ΜΟΝΟ τα κείμενα της νομοθεσίας από το lexifina/greece-law (Σύνταγμα, κώδικες, νόμοι, π.δ.).
#
#   tools/fetch_corpus.sh <φάκελος> [ref]      # ref: κλάδος ή commit, προεπιλογή main
#
# Το πλήρες αποθετήριο είναι δεκάδες GB (εκατομμύρια αποφάσεις Διαύγειας, νομολογία, σαρωμένες
# σελίδες ΦΕΚ). Με partial clone + sparse checkout κατεβαίνουν ~1 GB και μένουν ~650 MB κειμένου.
set -euo pipefail

DEST=${1:?"χρήση: tools/fetch_corpus.sh <φάκελος> [ref]"}
REF=${2:-main}
REPO=${CORPUS_REPO:-https://github.com/lexifina/greece-law}

if [ ! -d "$DEST/.git" ]; then
  git init -q "$DEST"
  git -C "$DEST" remote add origin "$REPO"
  git -C "$DEST" config remote.origin.promisor true
  git -C "$DEST" config remote.origin.partialclonefilter blob:none
  git -C "$DEST" config gc.auto 0
  # Ό,τι δεν είναι κείμενο νόμου μένει έξω: αποφάσεις, νομολογία, εικόνες σαρωμένων σελίδων.
  git -C "$DEST" sparse-checkout set --no-cone '/*' '!/caselaw/' '!/norm/' '!/local/' \
    '!/visualisations/' '!*.png' '!*.jpeg' '!*.jpg' '!*.gif' '!*.pdf'
fi

for attempt in 1 2 3; do
  if GIT_LFS_SKIP_SMUDGE=1 git -C "$DEST" fetch -q --depth 1 --filter=blob:none origin "$REF" \
     && GIT_LFS_SKIP_SMUDGE=1 git -C "$DEST" checkout -q --detach FETCH_HEAD; then
    echo "greece-law @ $(git -C "$DEST" rev-parse --short=12 HEAD) στο $DEST"
    exit 0
  fi
  echo "η λήψη διακόπηκε (προσπάθεια $attempt/3), ξαναδοκιμάζω…" >&2
  sleep $((attempt * 5))
done
echo "η λήψη του corpus απέτυχε" >&2
exit 1
