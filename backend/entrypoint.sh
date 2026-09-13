#!/bin/bash

# Exit on error
set -e

echo "🚀 Starting Django backend setup..."

# Wait for PostgreSQL to be ready
echo "⏳ Waiting for PostgreSQL to be ready..."
while ! nc -z $DB_HOST $DB_PORT; do
  sleep 0.5
done
echo "✅ PostgreSQL is ready!"

# Create migrations if they don't exist
echo "🔧 Creating migrations..."
python manage.py makemigrations --noinput || echo "ℹ️  No new migrations to create"

# Run migrations
echo "📦 Running database migrations..."
python manage.py migrate --noinput

# Create superuser if it doesn't exist
echo "👤 Checking for superuser..."
python manage.py shell << END
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='admin').exists():
    User.objects.create_superuser(
        username='admin',
        email='admin@aparsoft.com',
        password='admin123'
    )
    print('✅ Superuser created: admin / admin123')
else:
    print('ℹ️  Superuser already exists')
END

# Collect static files (without input)
echo "📁 Collecting static files..."
python manage.py collectstatic --noinput --clear || echo "⚠️  Static files collection skipped"

echo "🎉 Setup complete! Starting Django server..."
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Django Admin: http://localhost:8000/chatbot-admin/"
echo "  Username: admin"
echo "  Password: admin123"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Execute the main command (from Dockerfile CMD or docker-compose command)
exec "$@"
