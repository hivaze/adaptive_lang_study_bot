FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Claude CLI (required by claude-agent-sdk)
RUN curl -fsSL https://cli.claude.ai/install.sh | sh \
    || echo "Claude CLI install script not available; assuming pre-installed"

# Install Poetry
RUN pip install --no-cache-dir poetry

# Set working directory
WORKDIR /app

# Copy dependency files first for better caching
COPY pyproject.toml poetry.lock* ./

# Install dependencies (no dev dependencies in production)
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --only main --no-root

# Copy source code
COPY src/ src/
COPY alembic.ini ./

# Create non-root user (Claude CLI refuses bypassPermissions as root)
RUN useradd -m -s /bin/bash botuser && chown -R botuser:botuser /app
USER botuser

# Set Python path
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# Default command (overridden in docker-compose)
CMD ["python", "-m", "adaptive_lang_study_bot.entrypoints.run_bot"]
