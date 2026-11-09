FROM node:24-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.319
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && npm cache clean --force

USER node
WORKDIR /work
ENTRYPOINT ["claude"]
