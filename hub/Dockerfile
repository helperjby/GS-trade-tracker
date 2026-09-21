# arm64(파이4)/amd64 겸용. 빌드 컨텍스트는 hub/ 로 한정 — 관측기 코드(src/)는 이미지에 들어가지 않는다.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py db.py ./

EXPOSE 8800
# config.json 은 이미지에 넣지 않고 compose 가 ro 마운트한다
CMD ["python", "server.py", "--config", "/app/config.json"]
