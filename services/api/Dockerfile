FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY treadmill_api ./treadmill_api
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir .

EXPOSE 8088

CMD ["python", "-m", "treadmill_api.cli"]
