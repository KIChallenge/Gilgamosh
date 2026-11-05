FROM node:22-alpine AS assets
WORKDIR /build
COPY package.json package-lock.json tailwind.config.cjs ./
COPY src/gilgamosh/templates ./src/gilgamosh/templates
COPY src/gilgamosh/static ./src/gilgamosh/static
RUN npm ci && npm run build && cp node_modules/chart.js/dist/chart.umd.js src/gilgamosh/static/

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock requirements.lock README.md ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY --from=assets /build/src/gilgamosh/static/app.css ./src/gilgamosh/static/app.css
COPY --from=assets /build/src/gilgamosh/static/chart.umd.js ./src/gilgamosh/static/chart.umd.js
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && pip install --no-cache-dir --no-deps . && useradd --create-home app && chown -R app:app /app
USER app
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "8", "--timeout", "180", "gilgamosh:create_app()"]
