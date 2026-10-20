FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /ztp
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /ztp/requirements.txt
RUN pip install -r /ztp/requirements.txt

COPY app /ztp/app
COPY --chmod=755 fetch-cumulus-os /usr/local/bin/fetch-cumulus-os

EXPOSE 8080

CMD ["uvicorn", "app:app", "--app-dir", "/ztp/app", "--host", "0.0.0.0", "--port", "8080", "--log-config", "/ztp/app/logging.json"]
