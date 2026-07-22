#!/usr/bin/env bash
set -euo pipefail

# --- CONFIGURATION ---
BACKUP_DIR="/home/sfrontull/backups"   # Where to save backups
DB_NAME="litra"               # Database name
DB_USER="litra"              # Database user
KEEP_COUNT=3                         # Number of backups to retain

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# Timestamp for filename (e.g., mydatabase_2026-07-21_180000.sql.gz)
TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/${DB_NAME}_${TIMESTAMP}.sql.gz"

# 1. Take compressed backup using pg_dump
pg_dump -h localhost -U "$DB_USER" -d "$DB_NAME" -F p | gzip > "$BACKUP_FILE"

# 2. Prune old backups, keeping only the newest $KEEP_COUNT files
cd "$BACKUP_DIR"
ls -t ${DB_NAME}_*.sql.gz 2>/dev/null | tail -n +$((KEEP_COUNT + 1)) | xargs -r rm -f
