FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates ./templates
COPY static ./static

# SQLite 数据库文件目录（挂载为卷，刷新/容器重启后状态仍在）
RUN mkdir -p /data
ENV SIGNBOARD_DB=/data/signboard.db

EXPOSE 5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "4", "--timeout", "120", "app:app"]
