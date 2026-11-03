#!/bin/sh
# Seed the DSH home volume from the image, start the loopback-to-published
# forwarder, and hand off to dsh-superagent as the unprivileged `dsh` user.
#
# dsh-superagent requires a loopback Web server (`host: 127.0.0.1`), so the
# harness cannot listen on the container's external interface. socat bridges
# published port 3080 to 127.0.0.1:3082; compose publishes host
# 127.0.0.1:3082 -> container 3080, and the browser's Host header keeps saying
# 127.0.0.1:3082, which is what the management API's same-origin check wants.
set -eu

DSH_HOME="${DSH_HOME:-/home/dsh/.dsh}"
PROFILE="${DSH_PROFILE:-superagent}"
WEB_PORT="${DSH_WEB_PORT:-3082}"
PUBLISHED_PORT="${DSH_PUBLISHED_PORT:-3080}"
export DSH_HOME PROFILE

profile_dir="$DSH_HOME/profiles/$PROFILE"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DSH_HOME"
    chown dsh:dsh "$DSH_HOME" 2>/dev/null || true

    if [ ! -f "$profile_dir/package.json" ]; then
        echo "[entrypoint] seeding $DSH_HOME from the image"
        cp -a /opt/dsh-home/. "$DSH_HOME"/
        chown -R dsh:dsh "$DSH_HOME"
    fi

    # The bundle is linked into the profile by an absolute path inside the
    # image; repair the link for profiles seeded before that fix or copied by
    # hand (pnpm writes relative links that break on relocation).
    if [ -d "$profile_dir/node_modules" ]; then
        ln -sfn /opt/dsh-superagent "$profile_dir/node_modules/dsh-superagent" 2>/dev/null || true
        chown -h dsh:dsh "$profile_dir/node_modules/dsh-superagent" 2>/dev/null || true
    fi

    # The harness must stay loopback-only (dsh-superagent enforces it); repair
    # profiles seeded by an older image that pointed the webserver elsewhere.
    if [ -f "$profile_dir/cordis.patch.yml" ]; then
        sed -i -E "s/^([[:space:]]*)host:[[:space:]]*.*/\1host: 127.0.0.1/" "$profile_dir/cordis.patch.yml"
    fi

    mkdir -p "$DSH_HOME/state" "$DSH_HOME/gh-agent" /home/dsh/work
    chown -R dsh:dsh "$DSH_HOME/state" "$DSH_HOME/gh-agent" /home/dsh/work 2>/dev/null || true

    # Keep the published-port bridge alive for the lifetime of the container.
    (
        while :; do
            socat "TCP-LISTEN:${PUBLISHED_PORT},fork,reuseaddr,bind=0.0.0.0" \
                  "TCP:127.0.0.1:${WEB_PORT}" || sleep 1
        done
    ) &

    exec gosu dsh "$@"
fi

exec "$@"
