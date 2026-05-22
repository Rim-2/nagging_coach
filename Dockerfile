# 잔소리 코치 — 헤드리스 텔레그램 봇 컨테이너
#
# PC 활동 감시(Tracker)는 데스크톱·입력장치가 필요해 컨테이너에서 동작하지
# 않는다. ENABLE_PC_TRACKER=false 로 두고, 트래커 전용 패키지도 설치하지 않는다.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul

# 알람·리마인더가 한국 시간 기준으로 동작하도록 타임존 설정.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 설치 — 소스만 바뀔 때 pip 레이어 캐시를 재사용한다.
COPY requirements-docker.txt ./
RUN pip install --no-cache-dir -r requirements-docker.txt

# 앱 소스 복사 (.dockerignore 가 비밀·상태 파일을 제외).
COPY . .

# state.json 등 런타임 상태는 /data 볼륨에 둔다 (STATE_PATH 로 지정).
RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "-u", "app.py"]
