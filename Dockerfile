FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY sleeve_notes/ ./sleeve_notes/
COPY sleeve_notes_web/ ./sleeve_notes_web/
COPY webapp/ ./webapp/

RUN pip install .

WORKDIR /app/webapp

EXPOSE 8000

CMD python manage.py migrate --noinput && \
    exec gunicorn sleevenotes_app.wsgi:application \
        --bind 0.0.0.0:${PORT:-8000} \
        --workers 2 \
        --access-logfile - \
        --error-logfile -
