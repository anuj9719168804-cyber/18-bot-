# Use official Playwright Python image
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

# Ensure stdout and stderr are unbuffered so logs appear immediately in Render
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install ffmpeg for fast M3U8 stream conversion and video processing
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ensure matching Chromium browser is installed for current Playwright version
RUN playwright install chromium

# Copy project files
COPY . .

# Set default port
ENV PORT=10000
EXPOSE 10000

# Start bot and health check server
CMD ["python", "main.py"]
