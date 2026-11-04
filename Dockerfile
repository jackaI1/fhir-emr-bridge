FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home bridge
USER bridge
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "fhir_emr_bridge.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
