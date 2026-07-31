# Live/paper trading image. The [alpaca]/[polygon] extras (nautilus_trader==1.230.0
# pinned per Phase N0, alpaca-py, anthropic, desks' deps) are the only extras installed
# here — vectorbt/[research] is NEVER installed in this image (see
# wit/research/__init__.py and Dockerfile.research); a multi-hundred-MB research
# dependency has no business in the image that runs unattended against a real (paper)
# broker connection.
FROM python:3.12-slim AS base

WORKDIR /app
COPY pyproject.toml ./
COPY wit ./wit

# -e (editable): wit/config.py's PROJECT_ROOT is computed from __file__'s parent.parent -
# a non-editable install copies wit/ into site-packages, which resolves PROJECT_ROOT to
# site-packages instead of /app, breaking journal_path/decision_cache_path/kill_switch_file/
# dream state_path (none of which then land in the ../data:/app/data bind mount) and getting
# a PermissionError on boot besides, since the non-root `wit` user can't write site-packages.
# Editable keeps __file__ resolving under /app, where the source actually is.
RUN pip install --no-cache-dir -e ".[alpaca,polygon]"

# Non-root in the container too (defense in depth, separate from the VPS-level non-root
# docker-group user docs/BUILD_PLAN.md's Phase N8 section asks for on the host) — this
# process only ever initiates outbound connections (Anthropic, Alpaca, Polygon, yfinance/
# Finnhub/Alpha Vantage) and publishes no ports, so it has no legitimate reason to run as
# root. No local gateway container to depend on either (broker swap,
# docs/whatif-we-used-alpaca-quirky-aurora.md) - Alpaca/Polygon are both pure HTTPS/WSS.
#
# FUND_UID must match the deploy user's real uid (`id -u`) on the VPS (Phase N8 audit
# finding M2), passed via compose.yml's build.args - ../data is a bind mount, so it
# carries the HOST's ownership at runtime; the build-time chown below only sets ownership
# inside the image layer, which a bind mount then shadows. A uid mismatch means the `wit`
# user can't write data/ at all, including the kill switch - a safety mechanism that
# can't write its own halt file because of a uid mismatch is worse than none, since it
# looks like it's there.
ARG FUND_UID=1000
RUN useradd --create-home --uid ${FUND_UID} wit && \
    mkdir -p /app/data && chown -R wit:wit /app
USER wit

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["wit"]
# doctor, not paper/live, so a plain `docker run` (no compose, no explicit command) never
# accidentally boots a connected trading node - compose.yml overrides this explicitly.
CMD ["doctor"]

# `wit healthcheck`, not `wit status` or `wit doctor` (Phase N8 audit finding I2): status
# always exits 0 regardless of whether the node is trading, halted, or hung, so it can
# only detect a broken Python interpreter; doctor makes real Anthropic/Alpaca/Polygon
# calls when keys are present (broker swap addition - see wit/cli.py's module docstring),
# and a HEALTHCHECK firing every 60s would turn that into unbounded, silently-billed
# background API load (and eat into Polygon's 5/min free-tier budget besides).
# healthcheck instead checks the freshness of a
# heartbeat file FundStateActor's poll loop touches every cycle - a real liveness signal,
# still with no network call. compose.yml's fund service overrides CMD to ["paper"]; this
# HEALTHCHECK runs unmodified regardless, as a distinct process alongside that foreground one.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD wit healthcheck || exit 1
