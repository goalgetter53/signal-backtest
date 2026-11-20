FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
# mount a data dir to persist snapshots: docker run -v $PWD/data:/app/data signal-backtest collect
ENTRYPOINT ["signal-backtest"]
CMD ["--help"]
