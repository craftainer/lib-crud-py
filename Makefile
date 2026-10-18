# template-python-package's implementation of template-base's release
# contract (docs/TEMPLATE.md, "Release: a Makefile contract"). release.yml
# sets RELEASE_VERSION / RELEASE_TAG / RELEASE_ARCH -- see there. A pure-Python
# wheel is built once, on one arch. PYPI_PUBLISH (a repo variable) selects
# where `publish` uploads: unset = skip, `testpypi` or `pypi`. Uploads use
# PyPI trusted publishing (GitHub OIDC, the publish job's `id-token: write`),
# so there is no stored token.

.PHONY: release-arches build sbom release-assets publish release-check finalize

ARCHES := amd64
# Distribution name from pyproject.toml, so an instance that renames the
# package needs no change here.
NAME := $(shell python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["name"])')
ASSET := dist/$(NAME)-$(RELEASE_VERSION)

# renovate: datasource=docker depName=anchore/syft
SYFT_VERSION := v1.52.0

TESTPYPI_UPLOAD := https://test.pypi.org/legacy/
TESTPYPI_CHECK := https://test.pypi.org/simple/
PYPI_UPLOAD := https://upload.pypi.org/legacy/
PYPI_CHECK := https://pypi.org/simple/

release-arches:
	@python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' $(ARCHES)

# Stamps RELEASE_VERSION into pyproject.toml only for the duration of the
# build (restored on exit), so the checkout stays clean.
build:
	rm -rf dist
	mkdir -p dist
	@set -e; bak=$$(mktemp); cp pyproject.toml "$$bak"; \
	trap 'cp "$$bak" pyproject.toml; rm -f "$$bak"' EXIT; \
	$(if $(RELEASE_VERSION),uv version --frozen "$(RELEASE_VERSION)";) \
	uv build --no-sources

sbom:
	@set -e; whl=$$(ls dist/*.whl); \
	docker run --rm -v "$(CURDIR)/dist:/dist" anchore/syft:$(SYFT_VERSION) \
		"file:/$$whl" -o "spdx-json=/$${whl%.whl}.spdx.json"

release-assets:
	@set -e; for pat in 'dist/*.tar.gz' 'dist/*.whl' 'dist/*.spdx.json'; do \
		ls $$pat >/dev/null 2>&1 || { echo "missing release asset: $$pat" >&2; exit 1; }; \
	done; ls dist

publish:
ifeq ($(PYPI_PUBLISH),)
	@echo "PYPI_PUBLISH not set -- skipping publish."
else ifeq ($(PYPI_PUBLISH),pypi)
	uv publish --trusted-publishing always --publish-url $(PYPI_UPLOAD) --check-url $(PYPI_CHECK) dist/*.whl dist/*.tar.gz
else ifeq ($(PYPI_PUBLISH),testpypi)
	uv publish --trusted-publishing always --publish-url $(TESTPYPI_UPLOAD) --check-url $(TESTPYPI_CHECK) dist/*.whl dist/*.tar.gz
else
	@echo "PYPI_PUBLISH must be 'pypi' or 'testpypi', got '$(PYPI_PUBLISH)'." >&2; exit 1
endif

# Runs once after the arch leg, inside the devcontainer; dist/ holds the
# downloaded wheel. See scripts/release-check.sh.
release-check:
	scripts/release-check.sh

# Nothing to combine: a single wheel is built once.
finalize:
	@echo "Single pure-Python wheel -- nothing to finalize."
