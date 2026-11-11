# Small official Python image.
FROM python:3.12-slim

# Print logs straight away and don't write .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the application code (secrets are excluded by .dockerignore).
COPY app ./app

# Run as a normal user, not root. /app/data holds the SQLite database (a volume).
RUN useradd --create-home appuser \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data
USER appuser

CMD ["python", "-m", "app.main"]