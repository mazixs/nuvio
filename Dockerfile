FROM python:3.14-slim@sha256:0741d101873c12ab927e6f8653feb8862b9bd58771177acb1b885b95141f91b4 AS sqlite-builder

# Исправленная библиотека собирается отдельно, инструменты сборки не входят в рабочий образ.
RUN apt-get update && apt-get install -y --no-install-recommends gcc make libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python -c "import hashlib, urllib.request; data=urllib.request.urlopen('https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz', timeout=60).read(); assert hashlib.sha256(data).hexdigest() == '0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c'; open('/tmp/sqlite.tar.gz', 'wb').write(data)" \
    && tar -xzf /tmp/sqlite.tar.gz -C /tmp \
    && cd /tmp/sqlite-autoconf-3530400 \
    && ./configure --prefix=/opt/sqlite \
    && make -j2 && make install

FROM python:3.14-slim@sha256:0741d101873c12ab927e6f8653feb8862b9bd58771177acb1b885b95141f91b4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Обновляем системные пакеты из репозитория Debian: закрепленный базовый образ
# может содержать уязвимости, уже исправленные после его сборки.
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ffmpeg \
        openssl \
        libssl3t64 \
        openssl-provider-legacy \
        bsdutils \
        libblkid1 \
        liblastlog2-2 \
        libmount1 \
        libsmartcols1 \
        libuuid1 \
        login \
        mount \
        util-linux && \
    rm -rf /var/lib/apt/lists/*

# yt-dlp использует Deno для решения JavaScript-задач YouTube. Без него часть
# дорожек может отсутствовать даже при установленном yt-dlp-ejs.
COPY --from=denoland/deno:bin-2.9.7@sha256:bc5aa4466e21b6d3021226a85ba2e1911f7c386254d97b9d797903ab74edace2 /deno /usr/local/bin/deno

# Python должен загрузить именно библиотеку с исправленной гонкой WAL.
COPY --from=sqlite-builder /opt/sqlite/lib/libsqlite3.so* /usr/local/lib/
RUN ldconfig && python -c "import sqlite3; assert sqlite3.sqlite_version == '3.53.4'"

WORKDIR /app

COPY requirements.txt .
# pip нужен только при сборке. Его vendored-библиотеки не должны оставаться
# в рабочем образе и создавать лишние уязвимости.
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip uninstall --yes pip

COPY . .

RUN mkdir -p logs temp .secrets data

CMD ["python", "main.py"]
