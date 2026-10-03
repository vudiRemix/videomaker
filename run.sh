#!/usr/bin/env bash
# Запуск на macOS и Linux.
#   ./run.sh                 — обработать все видео из папки input
#   ./run.sh video.mp4 ...   — обработать указанные видео
#   ./run.sh --watch         — следить за папкой input и обрабатывать новые видео
cd "$(dirname "$0")" || exit 1

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg не найден. Установите его:"
    echo "  macOS:  brew install ffmpeg"
    echo "  Linux:  sudo apt install ffmpeg"
    exit 1
fi

exec python3 split_with_banner.py "$@"
