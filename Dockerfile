FROM python:3.12-slim

# CBC, the solver PuLP shells out to, needs no extra system packages on slim --
# pulp ships a bundled binary. Keeping this image dependency-free on purpose.

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fpl_*.py run_scheduler.py ./

# Run unprivileged. Nothing here needs root, and the mounted state/ volume
# holds a live credential -- no reason to hand a container root over it.
RUN useradd --create-home --uid 10001 fpl  && mkdir -p /app/state /app/reports  && chown -R fpl:fpl /app
USER fpl

# state/ holds your refresh token and caches. Mount it as a volume so the
# credential lives on your host, not baked into an image you might push.
VOLUME ["/app/state", "/app/reports"]

ENV PYTHONUNBUFFERED=1 TZ=UTC

# Default: the scheduler, running the full write-enabled daily job.
# Override for one-off commands, e.g.
#   docker compose run --rm fpl python fpl_token.py --set-refresh
#   docker compose run --rm fpl python fpl_model.py --squad
CMD ["python", "run_scheduler.py"]
