# joule:code JS/TS tools – built by joule:code on first use
# (tag joulecode/js-tools:<hash of this directory>). Runs without network.
FROM node:24-slim

WORKDIR /opt/joulecode
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund && npm cache clean --force
COPY tool.js cpuprofile.js eslint.config.mjs ./

ENV PATH="/opt/joulecode/node_modules/.bin:${PATH}"
