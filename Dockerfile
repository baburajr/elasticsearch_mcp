FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd -r -u 10001 esmcp && chown -R esmcp /app
USER esmcp

ENV ES_MCP_READ_ONLY=true
EXPOSE 8000
ENTRYPOINT ["elasticsearch-mcp"]
CMD ["--transport", "streamable-http"]
