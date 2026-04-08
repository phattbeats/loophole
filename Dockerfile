FROM python:3.12-slim

WORKDIR /app

# Install uv for fast package management
RUN pip install uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies (editable install for console script)
RUN uv sync --frozen --no-dev

# Create a non-root user for running the container
RUN adduser --disabled-password --gecos "" appuser && \
    mkdir -p /home/appuser/.config /home/appuser/sessions && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Copy application code (owned by appuser from above chown)
COPY loophole/ /app/loophole/
COPY examples/ /app/examples/
COPY config.yaml /app/

ENV PYTHONUNBUFFERED=1

# Run the CLI (accessible as 'loophole' on PATH after editable install)
CMD ["loophole"]
