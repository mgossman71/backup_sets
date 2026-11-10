# Test image: same tools the host uses (rsync, jq, kislyuk yq 3.1.0 as in Ubuntu 24.04),
# the portal's Python dependencies, pytest, and node + jsdom for the browser tests.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync jq util-linux procps nodejs npm \
 && rm -rf /var/lib/apt/lists/*
COPY portal/requirements.txt /tmp/portal-requirements.txt
RUN pip install --no-cache-dir -r /tmp/portal-requirements.txt yq==3.1.0 pytest==8.2.2
RUN mkdir -p /opt/js && cd /opt/js && npm init -y >/dev/null \
 && npm install --no-audit --no-fund --silent jsdom@24.1.0
ENV NODE_PATH=/opt/js/node_modules PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
CMD ["bash", "tests/run.sh"]
