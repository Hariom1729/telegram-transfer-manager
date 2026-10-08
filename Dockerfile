FROM python:3.12-slim

# Prevent Python from writing .pyc and buffer outputs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency definition and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure data and log directories exist
RUN mkdir -p /app/data /app/data/sessions /app/logs

# Set volume mounts for persistent data
VOLUME ["/app/data", "/app/logs"]

CMD ["python", "-m", "app.main"]

