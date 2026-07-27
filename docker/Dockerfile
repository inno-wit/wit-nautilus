# Live/paper trading image. The [ib] extra (nautilus_trader, anthropic, desks' deps) is
# pinned precisely once Phase N0 confirms the nautilus_trader version to target.
# vectorbt/[research] is NEVER installed here — see wit/research/__init__.py and the
# build plan §2 for why.
FROM python:3.12-slim AS base

WORKDIR /app
COPY pyproject.toml ./
COPY wit ./wit

RUN pip install --no-cache-dir ".[ib]"

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["wit"]
CMD ["doctor"]
