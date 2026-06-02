FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY treadmill_egress_proxy ./treadmill_egress_proxy

RUN pip install --no-cache-dir .

EXPOSE 3128

CMD ["python", "-m", "treadmill_egress_proxy"]
