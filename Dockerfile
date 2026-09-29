# Dockerfile — Budlance Reverse-Budget AI Travel Agent
# Uses uv for fast, reproducible dependency installation from uv.lock.

FROM python:3.12-slim

# System dependencies needed for Python C-extensions used by some packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv (official installer)
RUN curl -Ls https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:/root/.local/bin:$PATH"

WORKDIR /app

# Copy dependency manifest and lockfile first for layer caching
COPY pyproject.toml uv.lock ./

# Sync production dependencies (no dev group) into the system Python
RUN uv sync --no-dev --frozen

# Copy application source and static data
COPY src/ ./src/
COPY data/ ./data/

# Do NOT copy .env — secrets are injected at runtime via environment variables

# Expose the default port (overridable via PORT env var at runtime)
EXPOSE 8000

# Start uvicorn using the installed entry point.
# PORT and other settings are read from env vars by pydantic-settings.
CMD ["uv", "run", "uvicorn", "budlance.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
