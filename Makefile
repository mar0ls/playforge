.PHONY: build up down logs shell test test-fixture lab-regression clean

build:
	docker compose build

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
	  playforge:latest \
	  sh -c "pip install -q -U -r requirements-dev.txt && python -m pytest"

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

clean:
	docker compose down -v
	rm -rf data/projects/* data/app.db
