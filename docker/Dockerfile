FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

COPY docker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY docker/app.py .

RUN useradd -u 1001 appuser \
    && chown -R appuser:appuser /app

USER 1001

EXPOSE 8443
EXPOSE 9090

CMD ["python", "app.py"]
