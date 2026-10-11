# Production Dockerfile for Antigravity Telegram AI Executive Butler
# Compatible with Hugging Face Spaces, Render, Koyeb, and standard VPS
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=7860

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs under UID 1000
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files and configure permissions
COPY --chown=user:user . .
RUN mkdir -p /app/data && chown -R user:user /app

USER user
EXPOSE 7860

CMD ["python", "main.py"]
