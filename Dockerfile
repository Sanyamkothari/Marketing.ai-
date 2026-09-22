# The one image. The API container and the SageMaker job container are the same build; only the
# entrypoint argument differs, so what runs in a job is exactly what runs on a laptop (plan §12).
#
# Two things about this file are load-bearing and easy to get wrong:
#
#   1. The application runs from a SOURCE LAYOUT at /app with PYTHONPATH=/app, NOT `pip install`ed
#      into the virtualenv. `api/main.py` computes `UI_DIR = Path(__file__).parent.parent / "ui"`
#      and `engine/config.py` resolves the config root the same way, so an installed-package image
#      would silently look for both inside site-packages/ and serve a 404 for /ui. The wheel target
#      in pyproject.toml packages only engine/api/scripts - it does not carry ui/, configs/ or
#      templates/ - which is the same fact seen from the other side.
#   2. SageMaker's two job types launch a container differently. A Training job appends the literal
#      argument `train`; a Processing job replaces the entrypoint outright with whatever
#      `ContainerEntrypoint` says. `scripts/entrypoint.sh` therefore understands the bare word
#      `train`, and `SageMakerJobRunner` passes an explicit ContainerEntrypoint for processing.
#
# Third-party wheels are installed from `[project].dependencies` (plus the `aws` extra) with
# requirements-freeze.txt as a pip CONSTRAINTS file, so the image's versions are the repository's
# pinned versions rather than a second, independent resolution.

ARG PYTHON_BASE=python:3.11-slim

# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE} AS base

# `libgomp1` is not optional: lightgbm links against it, and without it `import lightgbm` dies with
# `OSError: libgomp.so.1: cannot open shared object file` (measured inside this image; the copies
# that scikit-learn and xgboost bundle have mangled SONAMEs and do not satisfy the lookup). It is a
# build ARG rather than a fixed line so the image can be built on a base that already carries it -
# pass `--build-arg RUNTIME_APT_PACKAGES=` and the layer is skipped.
ARG RUNTIME_APT_PACKAGES="libgomp1"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    MARKETING_AI_CONFIG_DIR=/app/configs \
    MARKETING_AI_DATA_DIR=/var/lib/marketing-ai

RUN if [ -n "${RUNTIME_APT_PACKAGES}" ]; then \
        apt-get update \
     && apt-get install -y --no-install-recommends ${RUNTIME_APT_PACKAGES} \
     && rm -rf /var/lib/apt/lists/*; \
    fi \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin marketing \
 && mkdir -p /var/lib/marketing-ai /app \
 && chown 10001:10001 /var/lib/marketing-ai

# ---------------------------------------------------------------------------
FROM base AS builder

ARG EXTRAS="aws"

RUN python -m venv /opt/venv
WORKDIR /src
COPY pyproject.toml requirements-freeze.txt /src/
COPY scripts/requirements_from_pyproject.py /src/scripts/
RUN python scripts/requirements_from_pyproject.py --extras "${EXTRAS}" > /tmp/requirements.txt

# The secret mount is for a build environment behind a TLS-terminating proxy; `required=false` means
# an ordinary build never has to supply one.
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=secret,id=pipca,target=/run/secrets/pipca,required=false \
    if [ -s /run/secrets/pipca ]; then export PIP_CERT=/run/secrets/pipca; fi; \
    pip install --only-binary=:all: -c requirements-freeze.txt -r /tmp/requirements.txt \
 && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /opt/venv -name '*.pyc' -delete

# ---------------------------------------------------------------------------
FROM base AS engine

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY engine    /app/engine
COPY scripts   /app/scripts
COPY configs   /app/configs
COPY templates /app/templates
COPY alembic.ini /app/alembic.ini
COPY alembic   /app/alembic
USER 10001
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["job"]

# ---------------------------------------------------------------------------
FROM engine AS api

USER root
COPY api /app/api
COPY ui  /app/ui
USER 10001
EXPOSE 8000

# `python`, not `curl`: a slim image has no curl, and adding one to serve a health check is a
# dependency bought for nothing. The 120-second start period is a CHOSEN default, not a measurement
# of this image's startup - AutoGluon's import is heavy, and the first real deployment should
# replace this number with one it timed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

CMD ["serve"]

# ---------------------------------------------------------------------------
# Built by `make image-test` and never shipped: it adds the dev extra and the test suite so the fast
# suite can run INSIDE the image, which is what catches a missing system library before a deployment
# does rather than after.
FROM api AS test

USER root
COPY pyproject.toml requirements-freeze.txt /app/
RUN python /app/scripts/requirements_from_pyproject.py --extras dev --pyproject /app/pyproject.toml > /tmp/dev.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=secret,id=pipca,target=/run/secrets/pipca,required=false \
    if [ -s /run/secrets/pipca ]; then export PIP_CERT=/run/secrets/pipca; fi; \
    pip install --only-binary=:all: -c /app/requirements-freeze.txt -r /tmp/dev.txt
COPY tests /app/tests
USER 10001
CMD ["pytest", "-m", "not slow", "-q"]
