# Optional/bonus packaging target -- not required by the assignment.
#
# Build:  docker build -t queuectl .
# Run (one-shot CLI command):
#   docker run --rm -v queuectl-data:/data -e QUEUECTL_DB=/data/queuectl.db queuectl enqueue "echo hi"
# Run (a long-lived worker container -- foreground is required in a
# container, since there's no shell left around to hold a detached
# background process alive):
#   docker run -d --name queuectl-worker -v queuectl-data:/data \
#     -e QUEUECTL_DB=/data/queuectl.db queuectl worker start --count 1 --foreground
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY queuectl ./queuectl
COPY README.md ./

RUN pip install --no-cache-dir .

VOLUME ["/data"]
ENV QUEUECTL_DB=/data/queuectl.db

ENTRYPOINT ["queuectl"]
CMD ["--help"]
