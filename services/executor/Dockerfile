FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

ENV EXECUTOR_HOST=0.0.0.0
EXPOSE 9000

# python -c avoids needing curl in the slim image.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
