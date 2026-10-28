#!/usr/bin/env bash
# Install backups on the server (docs/backups.md, section 4). Run: sudo /opt/aichat/ops/backup/install.sh
# Re-running is safe. The first pass creates /etc/aichat/restic.env from the template and asks you to
# fill it in; the second pulls the restic image, creates the repository (if missing) and enables the timers.
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

BACKUP_DIR=$AICHAT_LIB_DIR
UNITS=(aichat-backup.service aichat-backup.timer aichat-restore-test.service aichat-restore-test.timer)

((EUID == 0)) || die "Run as root: sudo $0"
# The systemd units run the scripts from this path.
[[ "$BACKUP_DIR" == /opt/aichat/ops/backup ]] \
    || die "The repository must be in /opt/aichat (the script is currently in $BACKUP_DIR)"

# jq is needed by the backup scripts; Ubuntu Server does not ship it by default. curl and openssl are present.
if ! command -v jq >/dev/null; then
    apt-get update -q
    apt-get install -y -q jq
fi
command -v docker >/dev/null || die "docker not found: install it first (docs/deploy.md, section 3)"

install -d -m 700 -o root -g root /etc/aichat /var/lib/aichat-backup /var/cache/aichat-restic

if [[ ! -e "$AICHAT_BACKUP_ENV" ]]; then
    install -m 600 -o root -g root "$BACKUP_DIR/restic.env.example" "$AICHAT_BACKUP_ENV"
    echo "Created $AICHAT_BACKUP_ENV from the template."
    echo "Fill it in (sudo nano $AICHAT_BACKUP_ENV) and run the installation again:"
    echo "  sudo $0"
    exit 0
fi
chown root:root "$AICHAT_BACKUP_ENV"
chmod 600 "$AICHAT_BACKUP_ENV"
# A password pasted by hand (docs/backups.md, 6.2) may have received default permissions.
if [[ -e "$RESTIC_PASSWORD_FILE" ]]; then
    chown root:root "$RESTIC_PASSWORD_FILE"
    chmod 600 "$RESTIC_PASSWORD_FILE"
fi

set -a
# shellcheck source=/dev/null
. "$AICHAT_BACKUP_ENV" || die "Could not read $AICHAT_BACKUP_ENV"
set +a
missing=()
for var in RESTIC_REPOSITORY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION; do
    [[ -n "${!var:-}" ]] || missing+=("$var")
done
((${#missing[@]} == 0)) || die "Not filled in $AICHAT_BACKUP_ENV: ${missing[*]}"
for var in HC_BACKUP_URL HC_RESTORE_URL; do
    [[ -n "${!var:-}" ]] || log "Warning: $var is not set - healthchecks.io pings will not be sent"
done

docker pull "restic/restic:$(<"$BACKUP_DIR/RESTIC_VERSION")"
"$BACKUP_DIR/init-repo.sh"

for unit in "${UNITS[@]}"; do
    install -m 644 -o root -g root "$BACKUP_DIR/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now aichat-backup.timer aichat-restore-test.timer
systemctl list-timers --no-pager 'aichat-*'
echo "Done: backup timers enabled."
