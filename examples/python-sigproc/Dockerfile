FROM python:3.12-slim
# Install dependencies at build time – containers have no network at run time.
RUN pip install --no-cache-dir pytest==8.3.3 numpy==2.1.3
WORKDIR /app
COPY . /app
ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1
