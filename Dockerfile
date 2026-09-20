# ITCS355 Lab 1 — training image
#
# Base pinned BY DIGEST, not by tag. Tags move: `python:3.11-slim` today is not
# `python:3.11-slim` next month, and a moving base is the commonest reason a
# "reproducible" build stops reproducing. This is the multi-arch index digest, so the
# same line resolves correctly on amd64 and arm64 while still naming exact bytes.
# Refresh it deliberately with:
#     docker buildx imagetools inspect python:3.11-slim
FROM python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Dependencies first so this layer caches independently of your source.
COPY requirements.txt ./
# --require-hashes turns a silently-substituted package into a build failure, which is
# what you want. It only works because requirements.txt is compiled with --generate-hashes.
RUN pip install --require-hashes --prefix=/install -r requirements.txt


FROM python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS runtime

# Non-root. A training container has no reason to run as root, and graders check.
RUN useradd --create-home --uid 10001 runner
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    GIT_PYTHON_REFRESH=quiet

COPY --from=builder /install /usr/local
WORKDIR /app
COPY --chown=runner:runner src/ ./src/
COPY --chown=runner:runner cloudlayer/ ./cloudlayer/
COPY --chown=runner:runner scripts/ ./scripts/

# MLflow writes model artifacts to ./mlruns relative to the working directory. The
# tracking DB goes to the mounted reports/ volume, but the artifact root does not, and
# /app belongs to root — so create it and hand it to runner rather than relaxing USER.
RUN mkdir -p /app/mlruns && chown runner:runner /app/mlruns

USER runner

# The image has no .git, so the commit it was built from is baked in here; runs inside
# the container log it as their git_commit. Last, so a new commit rebuilds only this layer.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

# Credentials NEVER enter an image layer. They arrive at runtime from SECRET_STORE_PATH
# or from the platform's identity. If you find yourself adding an ARG for a key, stop.
ENTRYPOINT ["python", "-m", "src.train"]
CMD ["--n-estimators", "200", "--max-depth", "8"]
