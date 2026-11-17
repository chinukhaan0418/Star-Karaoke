FROM python:3.12-slim
WORKDIR /app
COPY server.py .
COPY public ./public
ENV DB_PATH=/data/stars.db TRUST_PROXY=1 PYTHONUNBUFFERED=1
RUN mkdir -p /data
CMD ["python3", "server.py"]
