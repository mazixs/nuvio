FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

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

WORKDIR /app

COPY requirements.txt .
# pip нужен только при сборке. Его vendored-библиотеки не должны оставаться
# в рабочем образе и создавать лишние уязвимости.
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip uninstall --yes pip

COPY . .

RUN mkdir -p logs temp .secrets data

CMD ["python", "main.py"]
