FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gunicorn.conf.py .
COPY app ./app

EXPOSE 8443
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import ssl,urllib.request as u; u.urlopen('https://127.0.0.1:8443/healthz', context=ssl._create_unverified_context())" || exit 1
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:create_app()"]
