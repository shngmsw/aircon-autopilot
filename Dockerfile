# shngmsw-ubuntu（Docker 専用機）で常駐させるためのイメージ。
# ビルドと起動は手元の clone から `docker --context shngmsw-ubuntu` で行う（README「Docker での常駐」参照）。
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# .env と SQLite は bind mount で外から渡す（イメージに焼かない）
#   -v <host>/.env:/app/.env:ro   （load_dotenv() が /app/.env を読む）
#   -v <host>/data:/data          （DB_PATH で指す）
ENV DB_PATH=/data/aircon.db
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
