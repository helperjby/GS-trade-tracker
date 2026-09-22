# arm64(파이4)/amd64 겸용. 빌드 컨텍스트는 hub/ 로 한정 — 관측기 코드(src/)는 이미지에 들어가지 않는다.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py db.py devices.py ./

EXPOSE 8800
# DB 는 항상 볼륨(/data) — config.json 의 db_path 가 무엇이든 이 환경변수가 이긴다(server.load_config).
# 이게 없으면 기본 data/hub.db 가 컨테이너 임시 FS 에 생겨 `compose up --build` 마다 30일 데이터가 사라진다.
ENV HUB_DB_PATH=/data/hub.db
# config.json 은 이미지에 넣지 않고 compose 가 ro 마운트한다
CMD ["python", "server.py", "--config", "/app/config.json"]
