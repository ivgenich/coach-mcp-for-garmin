# garmin-mcp (github.com/Taxuspt/garmin_mcp) over Streamable HTTP on
# 127.0.0.1:8000, behind Caddy on Railway's $PORT. Caddy is only the edge and
# holds no secret: the Python server does the sign-in itself (OAuth 2.1 for the
# Claude apps, or the optional static bearer header MCP_BEARER_TOKEN).

# Caddy release binary (static Go build) from the official image.
FROM caddy:2.11.7 AS caddy

FROM python:3.12.15-slim

# Install from GitHub, not PyPI: the PyPI name "garmin-mcp" belongs to an
# unrelated project (Tyler-Irving/garmin-mcp), and upstream's only tag, 0.1.0
# (2026-05-08), predates the streamable-http transport and /healthz + /readyz.
# Pinned to the main commit of 2026-10-15; the tarball needs no git in the image.
# Upstream caps mcp<2, but only mcp 2.x serves protocol 2026-07-28, so
# garmin-mcp goes in with --no-deps and its dependencies (the list below is
# upstream's pyproject at this commit) go in beside mcp==2.3.0, every package
# pinned by constraints.txt. ext/mcp2_compat.py adapts mcp 2.x to the FastMCP
# API upstream calls. Bump GARMIN_MCP_REF, the list and constraints.txt together.
ARG GARMIN_MCP_REF=202913fde53c9d15890d661fc9748dc6d0d5af52

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY constraints.txt /tmp/constraints.txt
COPY config/garmin-key-client.txt /tmp/garmin-key-client.txt
RUN pip install --no-deps \
        "garmin-mcp @ https://github.com/Taxuspt/garmin_mcp/archive/${GARMIN_MCP_REF}.tar.gz" \
    && pip install -c /tmp/constraints.txt \
        "python-dotenv==1.2.2" \
        "garminconnect==0.3.2" \
        "requests==2.33.0" \
        "fitparse>=1.2.0" \
        "mcp==2.3.0" \
    && pip install --require-hashes --no-deps -r /tmp/garmin-key-client.txt

# The optional features' packages (ext/feature_config.py; both features are
# off until their variable is set). Tracing (TRACING=on, docs/tracing.md): the
# OpenTelemetry SDK and OTLP exporter, small, always installed so the switch
# needs no rebuild. History search (HISTORY_SEARCH=on, docs/history-search.md):
# LangChain, Postgres and the local embedding model's runtime, about 140 MB,
# installed only with the build argument HISTORY=1 (on Railway: the service
# variable HISTORY=1, which Railway passes as a build argument). Both files are
# the whole dependency closure with hashes, so --no-deps installs exactly them.
ARG HISTORY=0
COPY config/requirements-telemetry.txt /tmp/requirements-telemetry.txt
COPY config/requirements-history.txt /tmp/requirements-history.txt
RUN pip install --require-hashes --no-deps -r /tmp/requirements-telemetry.txt \
    && if [ "$HISTORY" = 1 ]; then pip install --require-hashes --no-deps -r /tmp/requirements-history.txt; fi

COPY --from=caddy /usr/bin/caddy /usr/bin/caddy

# Baked in so `railway ssh` and `docker compose exec` shells, which never run
# entrypoint.sh, see them too: garmin-mcp-auth then writes its tokens and their
# base64 copy to the /data volume instead of the container's throwaway home
# directory, and key mode caches its 24-hour access token there. /data is the
# volume, mounted at runtime over anything the build could put there. These
# names hold paths, not secrets; hadolint's DL3064 reads them as secrets, and
# its ignore line covers only the next instruction, so they stay in this one.
# hadolint ignore=DL3064
ENV GARMIN_MCP_TRANSPORT=streamable-http \
    GARMIN_MCP_HOST=127.0.0.1 \
    GARMIN_MCP_PORT=8000 \
    GARMIN_MCP_STATELESS_HTTP=true \
    GARMINTOKENS=/data/tokens \
    GARMINTOKENS_BASE64=/data/tokens/garmin_tokens.base64 \
    GARMIN_KEY_TOKEN_CACHE=/data/garmin-key/token.json

# This repo's extra tools (ext/README.md); .dockerignore leaves out ext/'s
# tests, fixtures and README. entrypoint.sh starts the server through
# launch.py.
#
# The .pth file makes every Python process (garmin-mcp-auth over railway ssh
# above all) serve upstream's `mcp.server.fastmcp` import from ext/mcp2_compat.py:
# at start it only registers an import hook, without adding /opt/garmin-ext to
# sys.path for good. The checks then fail the build, not the running server, if
# the adapter does not fit the installed mcp, garmin-mcp-auth cannot start, an
# upstream bump breaks a hook patch() or gate() wraps or a helper the status
# modules import (their selfchecks), config/enabled-tools.txt names a tool
# that is not read-only (allowed_tools), the built-in sign-in no longer fits
# the installed mcp (owner_auth.selfcheck() builds a throwaway sign-in on a
# temporary database and checks that every sign-in path is served), Garmin's
# Personal API client no longer has the classes, constants and errors key
# mode uses, or upstream no longer logs in through init_api(email, password)
# (key_mode.selfcheck()), or the status tools' time zone data is missing (any
# IANA zone; Europe/London is only a probe), or the optional features' modules
# no longer import with their pinned packages (tracing always; history search
# and its worker with HISTORY=1).
#
# The read-only tool list is baked in at a fixed path: launch.py reads
# ../config/enabled-tools.txt relative to itself, the repo's own layout, so
# /opt/config/ here. Env variables can only narrow it (ext/launch.py).
COPY ext/ /opt/garmin-ext/
COPY config/enabled-tools.txt /opt/config/enabled-tools.txt
RUN printf '%s\n' "import sys; sys.path.insert(0, '/opt/garmin-ext'); import mcp2_compat; sys.path.remove('/opt/garmin-ext'); mcp2_compat.register_finder()" \
        > "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/garmin_mcp2_compat.pth" \
    && garmin-mcp-auth --help >/dev/null \
    && python -c "import sys, zoneinfo; sys.path.insert(0, '/opt/garmin-ext'); import launch, owner_auth, key_mode, version; launch.patch(); launch.gate(launch.allowed_tools({})); owner_auth.selfcheck(); key_mode.selfcheck(); zoneinfo.ZoneInfo('Europe/London'); print('coach-mcp-for-garmin', version.VERSION)" \
    && python -c "import sys; sys.path.insert(0, '/opt/garmin-ext'); import telemetry, feature_config, garmin_guard; from opentelemetry.sdk.trace import TracerProvider; from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter" \
    && if [ "$HISTORY" = 1 ]; then python -c "import sys; sys.path.insert(0, '/opt/garmin-ext'); import psycopg, langchain_postgres, history_store, history_tools, garmin_guard_pg, jobqueue, worker, enqueue"; fi

COPY Caddyfile /etc/caddy/Caddyfile
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh \
    && caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

EXPOSE 8080
CMD ["/entrypoint.sh"]
