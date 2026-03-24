FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN useradd -u 1001 appuser
USER 1001

EXPOSE 8443

CMD ["python", "app.py"]
