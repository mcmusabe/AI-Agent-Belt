# Connect Smart - Dockerfile for Railway deployment
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies for Playwright and general use
RUN apt-get update && apt-get install -y \
    gcc \
    # Playwright dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (chromium only to save space)
RUN playwright install chromium

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Default port (Railway sets PORT env var)
ENV PORT=8000

# Set Playwright to use headless mode
ENV PLAYWRIGHT_HEADLESS=1

# Run the application
CMD uvicorn src.main:app --host 0.0.0.0 --port ${PORT}
