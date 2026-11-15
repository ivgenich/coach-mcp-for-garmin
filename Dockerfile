# garmin-mcp (github.com/Taxuspt/garmin_mcp) over Streamable HTTP on
# 127.0.0.1:8000, behind Caddy on Railway's $PORT, which requires
# "Authorization: Bearer $MCP_BEARER_TOKEN" on everything but /healthz and /readyz.

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
RUN pip install --no-deps \
        "garmin-mcp @ https://github.com/Taxuspt/garmin_mcp/archive/${GARMIN_MCP_REF}.tar.gz" \
    && pip install -c /tmp/constraints.txt \
        "python-dotenv==1.2.2" \
        "garminconnect==0.3.2" \
        "requests==2.33.0" \
        "fitparse>=1.2.0" \
        "mcp==2.3.0"

COPY --from=caddy /usr/bin/caddy /usr/bin/caddy

# Baked in so `railway ssh` shells, which never run entrypoint.sh, see them
# too: garmin-mcp-auth then writes its tokens to the /data volume instead of
# the container's throwaway home directory. /data is the Railway volume,
# mounted at runtime over anything the build could put there.
ENV GARMIN_MCP_TRANSPORT=streamable-http \
    GARMIN_MCP_HOST=127.0.0.1 \
    GARMIN_MCP_PORT=8000 \
    GARMIN_MCP_STATELESS_HTTP=true \
    GARMINTOKENS=/data/tokens

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
# that is not read-only (allowed_tools), or the status tools' time zone data
# is missing (any IANA zone; Europe/London is only a probe).
#
# The read-only tool list is baked in at a fixed path: launch.py reads
# ../config/enabled-tools.txt relative to itself, the repo's own layout, so
# /opt/config/ here. Env variables can only narrow it (ext/launch.py).
COPY ext/ /opt/garmin-ext/
COPY config/enabled-tools.txt /opt/config/enabled-tools.txt
RUN printf '%s\n' "import sys; sys.path.insert(0, '/opt/garmin-ext'); import mcp2_compat; sys.path.remove('/opt/garmin-ext'); mcp2_compat.register_finder()" \
        > "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/garmin_mcp2_compat.pth" \
    && garmin-mcp-auth --help >/dev/null \
    && python -c "import sys, zoneinfo; sys.path.insert(0, '/opt/garmin-ext'); import launch; launch.patch(); launch.gate(launch.allowed_tools({})); zoneinfo.ZoneInfo('Europe/London')"

COPY Caddyfile /etc/caddy/Caddyfile
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh \
    && caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

EXPOSE 8080
CMD ["/entrypoint.sh"]
