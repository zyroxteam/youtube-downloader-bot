FROM denoland/deno:bin-2.5.4 AS deno

FROM python:3.12-slim

# ffmpeg for yt-dlp merging; deno for yt-dlp YouTube JS challenges (EJS/PO)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "bot.py"]
