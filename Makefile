.PHONY: dev
dev:
	nix develop --extra-experimental-features flakes --extra-experimental-features nix-command

.PHONY: serve
serve:
	@echo "Starting Pod Service..."
	@echo "Feed URL: http://localhost:8083/feed.xml"
	@echo "Submit URLs at: http://localhost:8083"
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
	@nix develop --command treefmt

ARCHITECTURE_DIR ?= /tmp
ARCHITECTURE_SVG := $(ARCHITECTURE_DIR)/PodserviceArchitecture.svg
ARCHITECTURE_PNG := $(ARCHITECTURE_DIR)/PodserviceArchitecture.png
ARCHITECTURE_HTML := $(ARCHITECTURE_DIR)/PodserviceArchitecture.html

.PHONY: architecture
architecture: $(ARCHITECTURE_SVG) $(ARCHITECTURE_PNG) $(ARCHITECTURE_HTML)

$(ARCHITECTURE_SVG): README.md mermaid.json Makefile
	@awk '/^```mermaid/ { inside=1; next } inside && /^```/ { exit } inside { print }' README.md | nix develop --command mmdc -i - -o "$@" -t default -b white -c mermaid.json -w 1800 -H 1100

$(ARCHITECTURE_PNG): README.md mermaid.json Makefile
	@awk '/^```mermaid/ { inside=1; next } inside && /^```/ { exit } inside { print }' README.md | nix develop --command mmdc -i - -o "$@" -t default -b white -c mermaid.json -w 1800 -H 1100 -s 2

$(ARCHITECTURE_HTML): $(ARCHITECTURE_SVG) Makefile
	@printf '%s\n' \
		'<!doctype html>' \
		'<html lang="en">' \
		'<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Podservice architecture</title></head>' \
		'<body style="margin:0;background:white"><img src="PodserviceArchitecture.svg" alt="Podservice architecture" style="display:block;width:100vw;height:100vh;object-fit:contain"></body>' \
		'</html>' > "$@"

.PHONY: architecture-open
architecture-open: architecture
	@case "$$(uname -s)" in \
		Darwin) open "$(ARCHITECTURE_HTML)" ;; \
		Linux) xdg-open "$(ARCHITECTURE_HTML)" ;; \
		*) echo "Unsupported platform: $$(uname -s)"; exit 1 ;; \
	esac

.PHONY: update
update:
	$(eval LATEST_COMMIT := $(shell gh api repos/NixOS/nixpkgs/commits/master --jq '.sha'))
	@echo "Updating nixpkgs to $(LATEST_COMMIT)"
	@sed 's|nixpkgs.url = "github:NixOS/nixpkgs/.*"|nixpkgs.url = "github:NixOS/nixpkgs/$(LATEST_COMMIT)"|' flake.nix > flake.nix.tmp
	@mv flake.nix.tmp flake.nix

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
	@echo "  make architecture - Generate architecture SVG, PNG, and HTML under /tmp"
	@echo "  make architecture-open - Generate and open temporary architecture media"
	@echo ""
	@echo "Release:"
	@echo "  make update       - Update pinned nixpkgs to latest master"
	@echo "  make bump         - Bump version"
	@echo "  make release      - Create new release"
