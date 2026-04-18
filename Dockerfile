FROM n8nio/n8n:latest

# Install better-sqlite3 globally so Code nodes can require() it at runtime.
# Global install avoids n8n's pnpm-specific package.json (which uses
# "catalog:" protocol unsupported by npm).
USER root
RUN npm install -g --no-audit --no-fund better-sqlite3@^12
USER node
