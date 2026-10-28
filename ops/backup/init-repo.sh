#!/usr/bin/env bash
# Manual initialization of the restic repository (once at install time; the nightly backup does not run init).
# If there is no repository password, creates one. If the repository already exists, does nothing.
# The new password stays on disk (and the request to save it is printed) only after a successful
# restic init; in any other outcome it is deleted: otherwise a reinstall would leave
# a wrong password for an existing repository.
set -euo pipefail
# shellcheck source=ops/backup/lib.sh
. "$(dirname "$0")/lib.sh"

# Env file first: if it is faulty, no new password and no request to save it may remain.
load_env_file

PW_CREATED=0 # the password file was created by this run
PW_VALID=0   # the password is confirmed: restic init succeeded with it (or it opens the repository)

on_exit() {
    if ((PW_CREATED && !PW_VALID)); then rm -f "$RESTIC_PASSWORD_FILE"; fi
}

announce_password() {
    ((PW_CREATED)) || return 0
    echo "Repository password created: $RESTIC_PASSWORD_FILE"
    echo "Backups cannot be restored without it."
    echo "Save the password in a password manager: cat $RESTIC_PASSWORD_FILE"
}
trap on_exit EXIT

if [[ ! -e "$RESTIC_PASSWORD_FILE" ]]; then
    tmp="$RESTIC_PASSWORD_FILE.tmp"
    rm -f "$tmp"
    (
        umask 077
        mkdir -p "$(dirname "$RESTIC_PASSWORD_FILE")"
        openssl rand -base64 48 >"$tmp"
    )
    chmod 600 "$tmp"
    # Publish without overwriting: if the password file appeared in the meantime, leave it alone.
    if ! ln "$tmp" "$RESTIC_PASSWORD_FILE" 2>/dev/null; then
        rm -f "$tmp"
        die "Password file $RESTIC_PASSWORD_FILE appeared while running - run init-repo.sh again"
    fi
    PW_CREATED=1
    rm -f "$tmp"
fi

load_env

rc=0
repo_exists || rc=$?
case "$rc" in
    0)
        PW_VALID=1
        echo "Repository already exists, initialization is not needed."
        announce_password
        ;;
    1)
        restic_cmd init
        PW_VALID=1
        echo "Repository created."
        announce_password
        ;;
    *)
        if ((PW_CREATED)); then
            log "restic replied: $(printf '%s\n' "$REPO_EXISTS_ERROR" | head -n 1)"
            die "The repository seems to exist already (or access is denied). If this is a reinstall, put the previous password from your password manager into /etc/aichat/restic-password (docs/backups.md, section 6.2) and run again."
        fi
        die "Cannot access the repository: $(printf '%s\n' "$REPO_EXISTS_ERROR" | head -n 1)"
        ;;
esac
