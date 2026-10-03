#!/usr/bin/env python3
"""
Делит MP4-видео на части по одной минуте и в каждой части показывает
баннер на 15-й, 30-й и 45-й секунде.

Баннер — это видеоролик (mp4 со звуком) или картинка. Ролик накладывается
поверх основного видео и каждый раз проигрывается с начала, а его звук
подмешивается к звуку основного видео.

Примеры:
    python split_with_banner.py video.mp4 banner.mp4
    python split_with_banner.py video.mp4 banner.mp4 --width 0.4 --position bottom-right
    python split_with_banner.py video.mp4 banner.mp4 --chromakey          # убрать зелёный фон
    python split_with_banner.py video.mp4 banner.mp4 --main-volume 0.3    # приглушить видео под баннером
    python split_with_banner.py video.mp4 banner.png --image-duration 5
"""

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# Координаты баннера в выражениях фильтра overlay:
# W/H — размер видео, w/h — размер баннера, {m} — отступ от края.
POSITIONS = {
    "top": ("(W-w)/2", "{m}"),
    "bottom": ("(W-w)/2", "H-h-{m}"),
    "center": ("(W-w)/2", "(H-h)/2"),
    "top-left": ("{m}", "{m}"),
    "top-right": ("W-w-{m}", "{m}"),
    "bottom-left": ("{m}", "H-h-{m}"),
    "bottom-right": ("W-w-{m}", "H-h-{m}"),
    "full": ("(W-w)/2", "(H-h)/2"),
}


def die(message):
    print(f"Ошибка: {message}", file=sys.stderr)
    sys.exit(1)


def probe(path):
    """Читает через ffprobe размер, длительность и наличие звука."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,width,height:format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        die(f"не удалось прочитать файл {path}:\n{result.stderr.strip()}")
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        die(f"в файле {path} нет видеодорожки")
    return {
        "width": video["width"],
        "height": video["height"],
        "duration": float(info.get("format", {}).get("duration", 0) or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def fmt(seconds):
    return f"{seconds:.3f}".rstrip("0").rstrip(".")


def build_filter(args, video, banner, times, part_len):
    """Собирает filter_complex для одной части: наложение баннеров и сведение звука."""
    margin = round(video["height"] * args.margin)
    x_tpl, y_tpl = POSITIONS[args.position]
    x, y = x_tpl.format(m=margin), y_tpl.format(m=margin)
    shown = "+".join(f"between(t,{fmt(t)},{fmt(t + banner['duration'])})" for t in times)

    if args.position == "full":
        size = f"scale={video['width']}:{video['height']}:force_original_aspect_ratio=decrease"
    elif args.width:
        size = f"scale={round(video['width'] * args.width / 2) * 2}:-2"
    else:
        # Исходный размер, но не больше самого видео.
        size = (f"scale='min(iw,{video['width']})':'min(ih,{video['height']})'"
                f":force_original_aspect_ratio=decrease")

    graph = []
    last_video = "0:v"
    for i, t in enumerate(times, start=1):
        chain = f"[{i}:v]{size},format=rgba"
        if args.chromakey:
            chain += f",colorkey={args.chromakey}:{args.key_similarity}:0.1"
        # Сдвигаем начало ролика на нужную секунду части.
        graph.append(f"{chain},setpts=PTS-STARTPTS+{fmt(t)}/TB[b{i}]")
        graph.append(f"[{last_video}][b{i}]overlay=x={x}:y={y}:eof_action=pass[v{i}]")
        last_video = f"v{i}"
    graph.append(f"[{last_video}]format=yuv420p[v]")

    if video["has_audio"]:
        main_audio = "[0:a]"
        if times and args.main_volume != 1:
            main_audio += f"volume=volume={args.main_volume}:enable='{shown}',"
        graph.append(f"{main_audio}anull[a0]")
    else:
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={fmt(part_len)}[a0]")

    mix = ["[a0]"]
    if banner["has_audio"]:
        for i, t in enumerate(times, start=1):
            delay = round(t * 1000)
            graph.append(f"[{i}:a]asetpts=PTS-STARTPTS,volume={args.banner_volume},"
                         f"adelay={delay}:all=1[ba{i}]")
            mix.append(f"[ba{i}]")
    if len(mix) > 1:
        graph.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0[a]")
    else:
        graph.append("[a0]anull[a]")

    return ";".join(graph)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Делит MP4-видео поминутно и вставляет баннер (ролик со звуком или картинку) "
                    "на 15, 30 и 45 секунде каждой минуты.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="исходное видео (mp4)")
    parser.add_argument("banner", type=Path, help="баннер: видео (mp4 со звуком) или картинка (png/jpg)")
    parser.add_argument("-o", "--output-dir", type=Path,
                        help="папка для частей (по умолчанию <имя видео>_parts рядом с видео)")
    parser.add_argument("--times", default="15,30,45",
                        help="секунды внутри каждой части, когда запускается баннер")
    parser.add_argument("--segment", type=float, default=60,
                        help="длина одной части в секундах")
    parser.add_argument("--position", choices=POSITIONS, default="center",
                        help="где показывать баннер (full — растянуть на весь кадр)")
    parser.add_argument("--width", type=float,
                        help="ширина баннера в долях ширины видео, например 0.4 "
                             "(по умолчанию — исходный размер, но не больше кадра)")
    parser.add_argument("--margin", type=float, default=0.05,
                        help="отступ от края в долях высоты видео")
    parser.add_argument("--chromakey", nargs="?", const="0x00FF00", metavar="ЦВЕТ",
                        help="сделать прозрачным фон баннера этого цвета (без значения — зелёный)")
    parser.add_argument("--key-similarity", type=float, default=0.3,
                        help="насколько близкие к фону цвета тоже убирать (0.01–1)")
    parser.add_argument("--banner-volume", type=float, default=1.0,
                        help="громкость звука баннера (1 — как есть)")
    parser.add_argument("--main-volume", type=float, default=1.0,
                        help="громкость основного видео, пока идёт баннер (например 0.3)")
    parser.add_argument("--image-duration", type=float, default=5,
                        help="сколько секунд показывать баннер-картинку")
    parser.add_argument("--crf", type=int, default=20,
                        help="качество видео x264: меньше — лучше и тяжелее (18–28)")
    parser.add_argument("--preset", default="veryfast",
                        help="скорость кодирования x264 (ultrafast … veryslow)")
    return parser.parse_args()


def main():
    args = parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(f"не найден {tool}. Установите ffmpeg: https://ffmpeg.org/download.html")

    input_path = args.input.resolve()
    banner_path = args.banner.resolve()
    if not input_path.is_file():
        die(f"видео не найдено: {args.input}")
    if not banner_path.is_file():
        die(f"баннер не найден: {args.banner}")
    if args.segment <= 0:
        die("--segment должен быть больше нуля")

    try:
        times = sorted(float(t) for t in args.times.split(",") if t.strip())
    except ValueError:
        die(f"неверный формат --times: {args.times!r}, нужно например 15,30,45")
    if not times or any(not 0 <= t < args.segment for t in times):
        die(f"секунды в --times должны быть от 0 до {fmt(args.segment)}")

    video = probe(input_path)
    is_image = banner_path.suffix.lower() in IMAGE_EXTENSIONS
    if is_image:
        banner = {"duration": args.image_duration, "has_audio": False}
        banner_input = ["-loop", "1", "-framerate", "25",
                        "-t", fmt(args.image_duration), "-i", str(banner_path)]
    else:
        banner = probe(banner_path)
        banner_input = ["-i", str(banner_path)]
    if banner["duration"] <= 0:
        die(f"не удалось определить длительность баннера {args.banner}")

    total = video["duration"]
    # Хвост короче 0.1 с — это погрешность длительности, а не отдельная часть.
    parts = max(1, math.ceil((total - 0.1) / args.segment))

    output_dir = (args.output_dir or input_path.with_name(f"{input_path.stem}_parts")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Видео:  {input_path.name}, {video['width']}x{video['height']}, "
          f"{fmt(total)} с → частей: {parts}")
    print(f"Баннер: {banner_path.name}, {fmt(banner['duration'])} с, "
          f"{'со звуком' if banner['has_audio'] else 'без звука'}; "
          f"запуск на {', '.join(fmt(t) for t in times)} с каждой части")
    gaps = [b - a for a, b in zip(times, times[1:])]
    if gaps and banner["duration"] > min(gaps):
        print(f"Внимание: баннер ({fmt(banner['duration'])} с) длиннее промежутка между "
              f"показами ({fmt(min(gaps))} с) — показы будут накладываться друг на друга.")
    print(f"Сохраняю в: {output_dir}\n")

    for index in range(parts):
        start = index * args.segment
        part_len = min(args.segment, total - start)
        # В последней, короткой части баннер запускаем, только если он успевает начаться.
        part_times = [t for t in times if t < part_len]
        output = output_dir / f"{input_path.stem}_part_{index + 1:03d}.mp4"

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
               "-ss", fmt(start), "-t", fmt(part_len), "-i", str(input_path)]
        for _ in part_times:
            cmd += banner_input
        cmd += [
            "-filter_complex", build_filter(args, video, banner, part_times, part_len),
            "-map", "[v]", "-map", "[a]",
            "-t", fmt(part_len),
            "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(output),
        ]

        print(f"[{index + 1}/{parts}] {output.name} ({fmt(start)}–{fmt(start + part_len)} с)")
        if subprocess.run(cmd).returncode != 0:
            die(f"ffmpeg завершился с ошибкой на части {index + 1}")

    print(f"\nГотово! Создано частей: {parts} в папке {output_dir}")


if __name__ == "__main__":
    main()
