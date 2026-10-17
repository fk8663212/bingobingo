FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BINGO_DB=/app/data/bingo.sqlite3
WORKDIR /app
RUN groupadd --gid 10001 bingo && useradd --uid 10001 --gid bingo --no-create-home bingo \
    && mkdir -p /app/data /app/backups && chown -R bingo:bingo /app
COPY --chown=bingo:bingo bingo ./bingo
COPY --chown=bingo:bingo static ./static
COPY --chown=bingo:bingo app.py manage.py ./
COPY --chown=bingo:bingo scripts ./scripts
USER bingo
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
CMD ["python", "app.py"]
