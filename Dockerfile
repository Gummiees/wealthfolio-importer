FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
COPY wealthfolio_importer ./wealthfolio_importer

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1

CMD ["wealthfolio-importer", "watch"]

