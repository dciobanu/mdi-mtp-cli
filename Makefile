.DEFAULT_GOAL := help
.PHONY: help build install

UV ?= uv

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build: ## Build the wheel and sdist into dist/
	rm -rf dist
	$(UV) build

install: ## Install the mtp command for this user (uv tool, ~/.local/bin)
	$(UV) tool install --force --reinstall .
	@echo "Installed: $$(command -v mtp || echo 'mtp (add ~/.local/bin to PATH)')"
	@echo "Credentials: MTP_USERNAME/MTP_PASSWORD, ./.env, or ~/.config/mtp-cli/.env"
