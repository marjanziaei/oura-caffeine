#!/bin/bash
# Laptop side. Pulls a copy of the server's database into dated files and keeps the newest 30.
# Needs OURA_SERVER (user@host) in the environment; set in the launchd job, not in git.
set -euo pipefail
umask 077

: "${OURA_SERVER:?set OURA_SERVER=user@host}"
KEY="${OURA_BACKUP_KEY:-$HOME/.ssh/oura_backup_ed25519}"
DIR="${OURA_BACKUP_DIR:-$HOME/Backups/oura-caffeine}"
KEEP=30

mkdir -p "$DIR"; chmod 700 "$DIR"
tmp=$(mktemp "$DIR/.incoming.XXXXXX")
trap 'rm -f "$tmp"' EXIT
log() { echo "$(date '+%Y-%m-%d %H:%M:%S %Z') $*" >> "$DIR/backup.log"; }

if ! ssh -T -i "$KEY" -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes \
         -o ConnectTimeout=20 "$OURA_SERVER" > "$tmp" 2>> "$DIR/backup.log"; then
    log "FAILED: could not fetch the database"; exit 1
fi

# Only keep it if it is a healthy database that has our tables and some answers or nights in it.
check=$(sqlite3 "$tmp" "PRAGMA integrity_check;" 2>&1 | head -1)
tables=$(sqlite3 "$tmp" "SELECT count(*) FROM sqlite_master WHERE name IN ('daily_log','oura_nights');" 2>&1 || true)
rows=$(sqlite3 "$tmp" "SELECT (SELECT count(*) FROM daily_log) + (SELECT count(*) FROM oura_nights);" 2>&1 || true)
if [ "$check" != "ok" ] || [ "$tables" != "2" ] || ! [ "${rows:-0}" -gt 0 ] 2>/dev/null; then
    log "FAILED: fetched file did not pass checks (integrity=$check, tables=$tables)"; exit 1
fi

final="$DIR/oura-$(date '+%Y-%m-%d_%H%M').db"
mv "$tmp" "$final"; chmod 600 "$final"
log "OK: $(basename "$final") ($(wc -c < "$final" | tr -d ' ') bytes, $rows rows)"

# Prune only files that match our own naming, oldest first.
# (awk, because macOS head does not accept a negative line count.)
ls -1 "$DIR"/oura-????-??-??_????.db | sort | awk -v keep="$KEEP" '{f[NR]=$0} END{for(i=1;i<=NR-keep;i++) print f[i]}' | while read -r old; do rm -f "$old"; done
