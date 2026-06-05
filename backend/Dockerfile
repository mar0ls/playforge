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
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user. uid 1000 matches the host owner of the bind-mounted
# /data volume, so the app can still read/write its state without permission gymnastics.
RUN useradd --create-home --uid 1000 --shell /bin/bash app

# A system-wide git identity so `git merge`/`pull` (and any shelled-out git) never
# aborts for a missing committer identity. The app's own commits set an explicit
# author; this is the fallback for everything else.
RUN git config --system user.email "ansible-gui@localhost" \
    && git config --system user.name "Ansible GUI" \
    && git config --system init.defaultBranch main

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake popular Ansible collections into a system-wide path so `ansible-doc` (and
# thus the assistant's RAG/anti-hallucination layer) knows the modules people
# actually use (yum, ufw, authorized_key, ...) fully offline. See collections.yml.
COPY collections.yml .
RUN ansible-galaxy collection install -r collections.yml -p /usr/share/ansible/collections \
    && chmod -R a+rX /usr/share/ansible/collections
ENV ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections

COPY app ./app
RUN chown -R app:app /app

USER app

EXPOSE 8765

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
