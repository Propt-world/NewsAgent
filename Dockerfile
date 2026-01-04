# Use python 3.11-slim-bookworm for better compatibility
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

# 1. Install System Dependencies (Basic tools)
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 1.1 SSL Certificates (AWS DocumentDB)
COPY global-bundle.pem /app/certs/global-bundle.pem

# 2. Install Python Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy Application Code
COPY . .

CMD ["uvicorn", "src.main:api", "--host", "0.0.0.0", "--port", "8000"]