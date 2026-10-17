FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml
COPY app.py ./
EXPOSE 80
# --proxy-headers: Caddy terminates TLS in front of it.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "80", "--proxy-headers", "--forwarded-allow-ips", "*"]
