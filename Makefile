.PHONY: dev
dev:
	nix develop --extra-experimental-features flakes --extra-experimental-features nix-command

.PHONY: serve
serve:
	@echo "Starting Pod Service..."
	@echo "Feed URL: http://localhost:8083/feed.xml"
	@echo "Add URLs to: ./data/urls.txt"
	@nix develop --command python -m podservice serve --config config.example.yaml

.PHONY: info
info:
	@nix develop --command python -m podservice info --config config.example.yaml

.PHONY: clean
clean:
	@echo "Cleaning up cache files..."
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete
	@echo "Cleaned!"

.PHONY: clean-data
clean-data:
	@echo "WARNING: This will delete all audio files and data!"
	@echo "Press Ctrl+C to cancel, or Enter to continue..."
	@read
	@rm -rf data
	@echo "Data cleaned!"

.PHONY: test
test:
	@nix develop --command pytest tests/

.PHONY: format
format:
	@nix develop --command ruff check --fix .
	@nix develop --command ruff format .

.PHONY: bump
bump:
	$(eval LATEST_RELEASE := $(shell gh release list -L 1 | awk '{print $$1}' | sed 's/v//'))
	$(eval NEXT_RELEASE_VERSION := $(shell echo $(LATEST_RELEASE) | awk -F. '{$$NF = $$NF + 1;} 1' | sed 's/ /./g'))
	@echo "Updating version to $(NEXT_RELEASE_VERSION)"
	@sed 's/version = ".*"/version = "$(NEXT_RELEASE_VERSION)"/' pyproject.toml > pyproject.toml.tmp
	@mv pyproject.toml.tmp pyproject.toml

.PHONY: release
release:
	$(eval LATEST_RELEASE := $(shell gh release list -L 1 | awk '{print $$1}' | sed 's/v//'))
	$(eval NEXT_RELEASE_VERSION := $(shell echo $(LATEST_RELEASE) | awk -F. '{$$NF = $$NF + 1;} 1' | sed 's/ /./g'))
	@git add .
	@git commit -m "Update version to $(NEXT_RELEASE_VERSION)"
	@git push
	@gh release create v$(NEXT_RELEASE_VERSION) --generate-notes

.PHONY: help
help:
	@echo "Pod Service - Development Commands"
	@echo ""
	@echo "Development:"
	@echo "  make dev          - Enter nix development shell"
	@echo "  make serve        - Start service"
	@echo "  make info         - Show service configuration"
	@echo ""
	@echo "Maintenance:"
	@echo "  make clean        - Clean up temp files and cache"
	@echo "  make test         - Run tests"
	@echo "  make format       - Format code with ruff"
	@echo ""
	@echo "Release:"
	@echo "  make bump         - Bump version"
	@echo "  make release      - Create new release"
