import os
from celery import Celery
from celery.schedules import crontab
from django.conf import settings
from decouple import config


# Set the default Django settings module based on environment
if config("DJANGO_SETTINGS_MODULE"):
    django_settings_module = config("DJANGO_SETTINGS_MODULE")
else:
    django_settings_module = "config.settings.production"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", django_settings_module)
app = Celery("config")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django apps.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")


app.conf.update(
    broker_connection_retry_on_startup=True,
    broker_transport_options=settings.CELERY_BROKER_TRANSPORT_OPTIONS,
    result_backend_transport_options=settings.CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone=settings.TIME_ZONE,
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,  # 30 minutes
    worker_prefetch_multiplier=1,  # Prevents worker from taking too many tasks at once
    task_acks_late=True,  # Tasks are acknowledged after completion
)
