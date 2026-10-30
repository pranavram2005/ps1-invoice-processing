FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p data/uploads

EXPOSE 8000

# The image ships a pre-seeded SQLite database, so boot does no model work:
# `--if-empty` only fills the vendor and PO masters if they are somehow absent.
# On a free-tier host with an ephemeral filesystem that matters - a container
# that re-processed every sample invoice on each cold start would burn API
# credit for nothing.
CMD ["sh", "-c", "python -m backend.seed --if-empty && uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
