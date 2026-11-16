FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY config.json .
ENV SIGN_DATA_DIR=/data
ENV PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "src/web.py", "--host", "0.0.0.0", "--port", "8080", "--behind-proxy"]
