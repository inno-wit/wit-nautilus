# Live/paper trading image. The [ib] extra (nautilus_trader==1.230.0 pinned per Phase N0,
# anthropic, desks' deps) is the only extra installed here — vectorbt/[research] is NEVER
# installed in this image (see wit/research/__init__.py and Dockerfile.research); a
# multi-hundred-MB research dependency has no business in the image that runs unattended
# against a real (paper) broker connection.
FROM python:3.12-slim AS base

WORKDIR /app
COPY pyproject.toml ./
COPY wit ./wit

RUN pip install --no-cache-dir ".[ib]"

# Non-root in the container too (defense in depth, separate from the VPS-level non-root
# docker-group user docs/BUILD_PLAN.md's Phase N8 section asks for on the host) — this
# process only ever initiates outbound connections (Anthropic, IB Gateway, yfinance/
# Finnhub) and publishes no ports, so it has no legitimate reason to run as root.
RUN useradd --create-home --uid 1000 wit && \
    mkdir -p /app/data && chown -R wit:wit /app
USER wit

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["wit"]
# doctor, not paper/live, so a plain `docker run` (no compose, no explicit command) never
# accidentally boots a connected trading node - compose.yml overrides this explicitly.
CMD ["doctor"]

# `wit status` only, never `wit doctor`: doctor makes a real Anthropic API call when a
# key is present, which a HEALTHCHECK firing every 60s would turn into an unbounded,
# silently-billed background API load. status reads only local files (kill switch,
# config) - proves the process/image is alive, not that trading is healthy. compose.yml's
# fund service overrides CMD to ["paper"]; this HEALTHCHECK runs unmodified regardless, as
# a distinct process alongside that foreground one.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD wit status || exit 1
