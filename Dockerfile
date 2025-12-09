# Use a lightweight Python base image
FROM python:3.11-slim

# Set environment variables to optimize Python for Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

# 1. Install System Dependencies
# Added dependencies for Chromium (required by requests-html / pyppeteer)
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libxml2-dev \
    libxslt-dev \
    libjpeg-dev \
    zlib1g-dev \
    # Chromium dependencies
    chromium \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    wget \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Python Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copy Your Application Code
COPY . .

# (Optional) Default command, though docker-compose usually overrides this
CMD ["uvicorn", "src.main:api", "--host", "0.0.0.0", "--port", "8000"]