FROM node:22-alpine

ENV NODE_ENV=production
WORKDIR /app

COPY --chown=node:node package.json ./
COPY --chown=node:node src ./src
# DATA_PATH's directory in the Compose file, node's own, for a named volume
# or no mount at all (a bind mount keeps the host directory's owner).
RUN mkdir -p /var/lib/conduit-rooms && chown node:node /var/lib/conduit-rooms

USER node
EXPOSE 9130
# node:sqlite still prints an experimental warning on every start.
CMD ["node", "--disable-warning=ExperimentalWarning", "src/server.mjs"]
