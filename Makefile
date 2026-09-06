.PHONY: build lock up down logs shell test coverage api-contract test-fixture lab-regression backup restore clean

# The base compose file pulls the published image, so building from source goes
# through the dev overlay. Produces `playforge:dev`, the tag `make test` uses.
build:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml build

# Regenerate backend/requirements-lock.txt from requirements.txt. Runs in a linux
# container so the resolution matches the image, not the dev machine.
lock:
	docker run --rm -v $(PWD)/backend:/w -w /w python:3.12-slim sh -c "\
	  pip install -q pip-tools && \
	  pip-compile --generate-hashes --quiet --output-file=requirements-lock.txt requirements.txt"
	@echo "Wrote backend/requirements-lock.txt — rebuild with 'make build'."

# Pulls mar0ls/playforge; pin with PLAYFORGE_VERSION in .env.
up:
	docker compose up -d
	@echo "App: http://127.0.0.1:8765"

down:
	docker compose down

logs:
	docker compose logs -f app

shell:
	docker compose exec app bash

# Run the unit/integration suite inside the built image (has git + all deps).
# Requires `make build` first. Tests use a throwaway temp data dir, never /data.
test:
	docker run --rm \
	  -v $(PWD)/backend/tests:/app/tests:ro \
	  -v $(PWD)/backend/pytest.ini:/app/pytest.ini:ro \
	  -v $(PWD)/backend/requirements-dev.txt:/app/requirements-dev.txt:ro \
	  playforge:dev \
	  sh -c "pip install -q -U -r requirements-dev.txt && python -m pytest"

# Same suite with the coverage gate CI enforces, so a drop is visible before the
# push rather than in a red build. --cov-fail-under must match ci.yml.
coverage:
	docker run --rm \
	  -v $(PWD)/backend/tests:/app/tests:ro \
	  -v $(PWD)/backend/pytest.ini:/app/pytest.ini:ro \
	  -v $(PWD)/backend/requirements-dev.txt:/app/requirements-dev.txt:ro \
	  playforge:dev \
	  sh -c "pip install -q -U -r requirements-dev.txt && python -m pytest --cov=app --cov-report=term-missing:skip-covered --cov-fail-under=82"

# Regenerate tests/api_contract.json from the running app. Commit the result with
# the change that moved the surface: a regenerated snapshot in a diff is the cue
# to ask whether callers were considered.
api-contract:
	docker run --rm \
	  -v $(PWD)/backend/tests:/app/tests \
	  -v $(PWD)/backend/requirements-dev.txt:/app/requirements-dev.txt:ro \
	  playforge:dev \
	  sh -c "pip install -q -U -r requirements-dev.txt && python -c \"import json, sys; sys.path.insert(0, '/app/tests'); from test_api_contract import current; json.dump(current(), open('/app/tests/api_contract.json', 'w'), indent=2, sort_keys=True)\""
	@echo "Wrote backend/tests/api_contract.json"

# Zip the example project so it can be imported via the UI.
test-fixture:
	cd examples && rm -f hello-localhost.zip && zip -r hello-localhost.zip hello-localhost
	@echo "Created examples/hello-localhost.zip — import it in the UI."

lab-regression:
	@if [ -z "$(PROJECT_ID)" ]; then \
	  echo "PROJECT_ID is required, example: PROJECT_ID=<id> make lab-regression"; \
	  exit 2; \
	fi
	./scripts/lab_regression.sh

# Snapshot ./data (db + master key + project repos) to ./backups. Safe while running.
backup:
	./scripts/backup.sh

# Restore a snapshot: make restore ARCHIVE=backups/playforge-backup-*.tar.gz
# Stop the app first (`make down`).
restore:
	@if [ -z "$(ARCHIVE)" ]; then \
	  echo "ARCHIVE is required, example: ARCHIVE=backups/playforge-backup-0.1.0-*.tar.gz make restore"; \
	  exit 2; \
	fi
	./scripts/restore.sh $(ARCHIVE)

# Destroys local state. Take a `make backup` first — this is not recoverable.
clean:
	docker compose down -v
	rm -rf data/projects/* data/app.db
