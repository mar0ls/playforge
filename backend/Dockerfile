# hadolint global ignore=DL3002
# DL3002 (last USER must not be root) is a false positive here: we start as root
# only so the entrypoint can chown the bind-mounted /data, then drop to `app` via
# gosu — the same pattern the official postgres/redis images use. uvicorn (PID 1)
# runs as uid 1000, verified.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        sshpass \
        ca-certificates \
        curl \
        rsync \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user. uid 1000 matches the host owner of the bind-mounted
# /data volume, so the app can still read/write its state without permission gymnastics.
RUN useradd --create-home --uid 1000 --shell /bin/bash app

# A system-wide git identity so `git merge`/`pull` (and any shelled-out git) never
# aborts for a missing committer identity. The app's own commits set an explicit
# author; this is the fallback for everything else.
RUN git config --system user.email "playforge@localhost" \
    && git config --system user.name "Playforge" \
    && git config --system init.defaultBranch main

WORKDIR /app

# Install from the lock, not requirements.txt: the latter is floor-pinned (>=), so
# two builds of the same git tag could otherwise ship different dependency versions.
# --require-hashes also fails the build if a package doesn't match its recorded hash.
# Regenerate after editing requirements.txt: make lock
COPY requirements.txt requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

# Bake popular Ansible collections into a system-wide path so `ansible-doc` (and
# thus the assistant's RAG/anti-hallucination layer) knows the modules people
# actually use (yum, ufw, authorized_key, ...) fully offline. See collections.yml.
COPY collections.yml .
RUN ansible-galaxy collection install -r collections.yml -p /usr/share/ansible/collections \
    && chmod -R a+rX /usr/share/ansible/collections
ENV ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections

# Monaco, vendored so the editor works with no internet (the rest of the frontend
# is already local). Kept outside /app/app because the dev compose override
# bind-mounts the host's backend/app over it, which would hide anything baked in
# there. Served at /vendor by main.py; ~14 MB for min/vs alone (the full package
# is ~80 MB, hence min/ only).
ARG MONACO_VERSION=0.52.0
ARG MONACO_SHA256=5dbda58750b13452de39affcea703af096022d8e3546c67ed5d3bd072763f939
RUN set -eux; \
    curl -fsSL "https://registry.npmjs.org/monaco-editor/-/monaco-editor-${MONACO_VERSION}.tgz" -o /tmp/monaco.tgz; \
    echo "${MONACO_SHA256}  /tmp/monaco.tgz" | sha256sum -c -; \
    mkdir -p /opt/playforge/vendor/monaco; \
    tar xzf /tmp/monaco.tgz -C /tmp; \
    cp -r /tmp/package/min/vs /opt/playforge/vendor/monaco/vs; \
    cp /tmp/package/LICENSE /opt/playforge/vendor/monaco/LICENSE; \
    rm -rf /tmp/monaco.tgz /tmp/package; \
    chmod -R a+rX /opt/playforge/vendor

COPY app ./app
RUN chown -R app:app /app

# Start as root so the entrypoint can fix bind-mounted /data ownership, then it
# drops to the `app` user via gosu. (No `USER app` — the entrypoint handles it.)
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8765

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
