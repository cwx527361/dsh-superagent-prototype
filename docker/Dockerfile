# syntax=docker/dockerfile:1.7
# dsh-superagent, containerized.
#
# The image contains everything docs/006-deployment.md asks for on a host:
# Node.js, pnpm, git, tmux, python3, the GitHub CLI and the DeepSeek Harness
# CLI, plus this bundle built and linked into a `superagent` profile. On first
# start the profile is copied out of the image into the DSH_HOME volume.
#
# Build from the repository root:
#   docker build -f docker/Dockerfile -t dsh-superagent:local .

FROM node:24-bookworm

ARG DSH_VERSION=0.2.1-rc.3
ARG PNPM_VERSION=11.7.0

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai

### System prerequisites (docs/006-deployment.md "Prerequisites") ############
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        git-lfs \
        gosu \
        jq \
        less \
        procps \
        python3 \
        socat \
        tini \
        tmux \
        unzip \
 && rm -rf /var/lib/apt/lists/*

# GitHub CLI 2.48+; Debian bookworm ships no `gh` package.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

### DeepSeek Harness CLI and pnpm #############################################
# node-pty, koffi and dsh-subprocess-local need their install scripts to run.
RUN npm config set allow-scripts=@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs \
 && npm install -g @deepseek-ai/dsh@${DSH_VERSION} pnpm@${PNPM_VERSION} \
 && npm cache clean --force

### Build this bundle #########################################################
WORKDIR /opt/dsh-superagent
COPY package.json pnpm-lock.yaml tsconfig.json tsdown.config.ts ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm build

# M1 POC: the runner hub is a tiny standalone Node service with its own deps.
RUN npm --prefix /opt/dsh-superagent/runner/hub install --omit=dev --no-audit --no-fund

### Bake a seeded DSH_HOME ####################################################
# The runtime volume starts empty; the entrypoint copies this seed into it so
# the first boot needs no network and no extra commands.
ENV DSH_HOME=/opt/dsh-home
# pnpm links the profile dependency relatively; make it absolute so the seed
# still resolves after it is copied into the runtime DSH_HOME volume.
RUN dsh superagent --from-default-profile web --dump-config > /dev/null \
 && dsh plugin --profile superagent add "link:/opt/dsh-superagent" \
 && ln -sfn /opt/dsh-superagent /opt/dsh-home/profiles/superagent/node_modules/dsh-superagent
COPY docker/profile.cordis.patch.yml /opt/dsh-home/profiles/superagent/cordis.patch.yml

### Runtime user ##############################################################
RUN usermod -l dsh -d /home/dsh -m node \
 && groupmod -n dsh node \
 && mkdir -p /home/dsh/.local/bin /home/dsh/work \
 && printf '#!/bin/sh\nGH_CONFIG_DIR=/home/dsh/.dsh/gh-agent exec gh "$@"\n' \
        > /home/dsh/.local/bin/gh-agent \
 && chmod +x /home/dsh/.local/bin/gh-agent \
 && chown -R dsh:dsh /home/dsh /opt/dsh-home /opt/dsh-superagent

ENV DSH_HOME=/home/dsh/.dsh \
    DSH_PROFILE=superagent \
    PATH=/home/dsh/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

COPY docker/poc /opt/poc
COPY docker/entrypoint.sh /usr/local/bin/dsh-superagent-entrypoint
COPY docker/healthcheck.sh /usr/local/bin/dsh-superagent-healthcheck
RUN chmod +x /usr/local/bin/dsh-superagent-entrypoint /usr/local/bin/dsh-superagent-healthcheck

WORKDIR /home/dsh
EXPOSE 3082
# Run through `scripts/dsh-superagent` semantics: one foreground DSH process;
# the container is its supervisor, so `docker compose restart` replaces it.
CMD ["dsh", "--profile", "superagent"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/dsh-superagent-entrypoint"]
