# Backend Dockerfile for Django 5.2
FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client \
    netcat-openbsd \
    gcc \
    python3-dev \
    musl-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy entrypoint scripts
COPY entrypoint.sh /app/entrypoint.sh
COPY entrypoint-celery.sh /app/entrypoint-celery.sh
RUN chmod +x /app/entrypoint.sh /app/entrypoint-celery.sh

# Copy project files
COPY . /app/

# Create directory for static files
RUN mkdir -p /app/staticfiles /app/media

# Expose port
EXPOSE 8000

# Set entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]

# Default command
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
