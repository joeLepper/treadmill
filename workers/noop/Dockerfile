FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir boto3==1.43.5

COPY noop_worker /app/noop_worker

CMD ["python", "-m", "noop_worker"]
