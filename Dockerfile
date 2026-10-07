FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Run as an unprivileged user; /srv/data (SQLite DB + PDFs) is the only writable path.
RUN useradd --system --no-create-home appuser && mkdir -p /srv/data && chown appuser /srv/data
USER appuser
VOLUME /srv/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health')"
# One process on purpose: background workers + startup recovery assume a single instance.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
