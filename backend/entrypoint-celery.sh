#!/bin/bash

# Celery entrypoint - NO migrations, just wait for backend to be ready
set -e

echo "🔄 Celery worker starting..."

# Wait for PostgreSQL to be ready
echo "⏳ Waiting for PostgreSQL..."
while ! nc -z $DB_HOST $DB_PORT; do
  sleep 0.5
done
echo "✅ PostgreSQL is ready!"

# Wait a bit more for backend to finish migrations
echo "⏳ Waiting for migrations to complete..."
sleep 10

echo "🎉 Starting Celery worker..."
exec "$@"
