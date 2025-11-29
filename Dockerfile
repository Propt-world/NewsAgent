# Use a lightweight Python base image
FROM python:3.11-slim

# Set environment variables to optimize Python for Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

# 1. Install System Dependencies
# These are NOT Python packages. These are Linux libraries required by
# 'lxml' and 'newspaper4k' to compile correctly.
# 'langgraph' does not need these, but 'newspaper4k' does.
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libxml2-dev \
    libxslt-dev \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Python Dependencies
# This single line installs EVERYTHING in your requirements.txt
# (beautifulsoup4, fastapi, langgraph, redis, etc.)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copy Your Application Code
COPY . .

# (Optional) Default command, though docker-compose usually overrides this
CMD ["uvicorn", "src.main:api", "--host", "0.0.0.0", "--port", "8000"]