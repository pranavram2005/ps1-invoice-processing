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

# Just serve. The app seeds itself: `_startup()` in main.py calls
# `seed_masters_if_empty()`, which fills the vendor and PO masters only when
# they are absent and is a no-op otherwise. The image also ships an already
# seeded database, so a cold start does no model work and costs no API credit -
# which matters on a free-tier host, where every restart is a cold start.
#
# An earlier CMD ran `python -m backend.seed --if-empty &&` first. That flag
# does not exist: argparse exited 2, the `&&` short-circuited, uvicorn never
# started and the deploy failed. The step was redundant with startup anyway,
# and dropping it removes both the failure mode and a whole interpreter launch
# from every cold start.
#
# `exec` hands PID 1 to uvicorn so it receives SIGTERM directly and shuts down
# gracefully rather than being killed when the host stops the container.
CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
