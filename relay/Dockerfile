FROM node:22-alpine

ENV NODE_ENV=production
WORKDIR /app

COPY --chown=node:node package.json ./
COPY --chown=node:node src ./src

USER node
EXPOSE 9120
CMD ["node", "src/server.mjs"]
