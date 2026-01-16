FROM python:3.9-slim
RUN pip install kubernetes
COPY queue-controller.py /queue-controller.py
CMD ["python", "-u", "/queue-controller.py"]
