# Image for the backup script tests: pinned restic version + bash and utilities.
# RESTIC_VERSION is passed by compose.yml (value from ops/backup/RESTIC_VERSION).
ARG RESTIC_VERSION
FROM pgsty/mc:RELEASE.2026-09-16T00-00-00Z AS mc

FROM restic/restic:${RESTIC_VERSION}
# Packages from the Alpine branch of the pinned restic image; exact -rN versions are not pinned (Alpine removes them).
RUN apk add --no-cache \
        bash~5.3 \
        coreutils~9 \
        curl~8 \
        jq~1.8 \
        openssl~3.5 \
        postgresql16-client~16 \
        python3~3.14 \
        shellcheck~0.11
COPY --from=mc /usr/bin/mc /usr/local/bin/mc
RUN mkdir -p /work /tmp/hc
WORKDIR /work
ENTRYPOINT []
CMD ["bash"]
