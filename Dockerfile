FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8895 FLIPBOOK_DATA_DIR=/app/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-noto-cjk libharfbuzz0b libfreetype6 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home studio && mkdir -p /app/data && chown studio:studio /app/data
COPY --chown=studio:studio app.py ./
COPY --chown=studio:studio flipbook ./flipbook
COPY --chown=studio:studio static ./static
COPY --chown=studio:studio templates ./templates
USER studio
EXPOSE 8895
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8895/api/health',timeout=2).read()"
CMD ["python", "app.py"]
