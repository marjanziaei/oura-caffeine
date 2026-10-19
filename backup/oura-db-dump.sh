#!/bin/sh
# Server side. Prints a consistent copy of the database to stdout and nothing else.
# Installed as /usr/local/bin/oura-db-dump (owned by root) and used as the forced command
# of the backup-only SSH key, so that key can do nothing but run this.
set -eu
umask 077
tmp=$(mktemp /tmp/oura-dump.XXXXXX)
trap 'rm -f "$tmp"' EXIT
# .backup is safe while the bot is writing; a plain file copy could catch a half-written page.
sqlite3 /home/ubuntu/oura-caffeine/sleep_caffeine.db ".backup '$tmp'"
cat "$tmp"
