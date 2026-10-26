FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY discover discover
COPY tests tests
RUN DISCOVER_DATA=/tmp/testdata python -m tests && rm -rf /tmp/testdata && useradd -u 1000 -m app && mkdir -p /data && chown app /data
USER app
EXPOSE 5050
CMD ["uvicorn", "discover.app:app", "--host", "0.0.0.0", "--port", "5050", "--proxy-headers"]
