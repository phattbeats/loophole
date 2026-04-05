FROM python:3.12-slim

WORKDIR /app

# Install uv for fast package management
RUN pip install uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY loophole/ ./loophole/
COPY examples/ ./examples/
COPY config.yaml ./

# Create sessions directory
RUN mkdir -p /app/sessions

ENV PYTHONUNBUFFERED=1

# Run the CLI
CMD ["uv", "run", "python", "-m", "loophole.main"]
