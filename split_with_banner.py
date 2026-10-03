#!/usr/bin/env python3
"""
Делит видео на части по одной минуте и в каждой части показывает
баннер на 15-й, 30-й и 45-й секунде.

Баннер — это видеоролик (mp4 со звуком) или картинка. Ролик накладывается
поверх основного видео и каждый раз проигрывается с начала, а его звук
подмешивается к звуку основного видео. Если у баннера зелёный (или синий)
фон, он определяется и вырезается автоматически.

Обычная работа:
    1. Положить видео в папку input
    2. Запустить:  python split_with_banner.py   (на Windows — run.bat)
    3. Забрать части из папки output/<имя видео>/

Ещё примеры:
    python split_with_banner.py video1.mp4 video2.mp4      # конкретные файлы
    python split_with_banner.py --watch                     # следить за папкой input
    python split_with_banner.py --banner other.mp4 --width 0.4 --position bottom-right
    python split_with_banner.py --main-volume 0.3           # приглушить видео под баннером
"""

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
DONE_MARKER = ".done"
WATCH_INTERVAL = 5

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


class VideoError(Exception):
    pass


def die(message):
    print(f"Ошибка: {message}", file=sys.stderr)
    sys.exit(1)


def fmt(seconds):
    return f"{seconds:.3f}".rstrip("0").rstrip(".")


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
        raise VideoError(f"не удалось прочитать файл {path.name}: {result.stderr.strip()}")
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise VideoError(f"в файле {path.name} нет видеодорожки")
    return {
        "width": video["width"],
        "height": video["height"],
        "duration": float(info.get("format", {}).get("duration", 0) or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def sample_color(path, x, y, is_image):
    """Средний цвет квадрата 4×4 пикселя в точке (x, y) первого кадра."""
    seek = [] if is_image else ["-ss", "0.1"]
    result = subprocess.run(
        ["ffmpeg", "-v", "error", *seek, "-i", str(path), "-frames:v", "1",
         "-vf", f"format=rgb24,crop=4:4:{x}:{y},scale=1:1", "-f", "rawvideo", "-"],
        capture_output=True,
    )
    return tuple(result.stdout[:3]) if len(result.stdout) >= 3 else None


def detect_key_color(path, banner, is_image):
    """Ищет однотонный зелёный или синий фон по углу баннера. Возвращает цвет или None."""
    corner = sample_color(path, 2, 2, is_image)
    center = sample_color(path, banner["width"] // 2 - 2, banner["height"] // 2 - 2, is_image)
    if corner is None or center is None:
        return None
    r, g, b = corner
    is_green = g > 100 and g > r + 50 and g > b + 50
    is_blue = b > 100 and b > r + 50 and b > g + 50
    # Если и центр того же цвета — это не фон, а сам баннер такой.
    if (is_green or is_blue) and sum(abs(c1 - c2) for c1, c2 in zip(corner, center)) > 90:
        return f"0x{r:02X}{g:02X}{b:02X}"
    return None


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
        chain = f"[{i}:v]"
        if banner["key_color"]:
            chain += f"chromakey={banner['key_color']}:{args.key_similarity}:0.05,"
        # Сдвигаем начало ролика на нужную секунду части.
        graph.append(f"{chain}{size},setpts=PTS-STARTPTS+{fmt(t)}/TB[b{i}]")
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


def load_banner(args):
    path = args.banner.resolve()
    if not path.is_file():
        die(f"баннер не найден: {args.banner}\n"
            f"Положите файл баннера рядом со скриптом под именем banner.mp4 или укажите --banner")
    is_image = path.suffix.lower() in IMAGE_EXTENSIONS
    try:
        banner = probe(path)
    except VideoError as error:
        die(str(error))
    if is_image:
        banner["duration"] = args.image_duration
        banner["has_audio"] = False
        banner["input"] = ["-loop", "1", "-framerate", "25",
                           "-t", fmt(args.image_duration), "-i", str(path)]
    else:
        banner["input"] = ["-i", str(path)]
    if banner["duration"] <= 0:
        die(f"не удалось определить длительность баннера {path.name}")

    if args.chromakey == "auto":
        banner["key_color"] = detect_key_color(path, banner, is_image)
    elif args.chromakey == "off":
        banner["key_color"] = None
    else:
        banner["key_color"] = args.chromakey
    banner["path"] = path
    return banner


def output_dir_for(video_path, args):
    return args.output_dir / video_path.stem


def is_done(video_path, args):
    return not args.force and (output_dir_for(video_path, args) / DONE_MARKER).exists()


def process_video(video_path, banner, times, args):
    """Режет одно видео на части с баннером. Возвращает True, если всё получилось."""
    try:
        video = probe(video_path)
    except VideoError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return False

    total = video["duration"]
    # Хвост короче 0.1 с — это погрешность длительности, а не отдельная часть.
    parts = max(1, math.ceil((total - 0.1) / args.segment))
    out_dir = output_dir_for(video_path, args)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / DONE_MARKER).unlink(missing_ok=True)

    print(f"\n=== {video_path.name}: {video['width']}x{video['height']}, "
          f"{fmt(total)} с → частей: {parts}")
    print(f"Сохраняю в: {out_dir}")

    for index in range(parts):
        start = index * args.segment
        part_len = min(args.segment, total - start)
        # В последней, короткой части баннер запускаем, только если он успевает начаться.
        part_times = [t for t in times if t < part_len]
        output = out_dir / f"{video_path.stem}_part_{index + 1:03d}.mp4"

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
               "-ss", fmt(start), "-t", fmt(part_len), "-i", str(video_path)]
        for _ in part_times:
            cmd += banner["input"]
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
            print(f"Ошибка: ffmpeg не справился с частью {index + 1} видео {video_path.name}",
                  file=sys.stderr)
            return False

    (out_dir / DONE_MARKER).write_text(f"{video_path.name}: частей: {parts}\n", encoding="utf-8")
    print(f"Готово: {video_path.name} → частей: {parts}")
    return True


def find_videos(paths, banner_path):
    """Собирает видеофайлы из указанных файлов и папок (папки — без подпапок)."""
    found = []
    for path in paths:
        if path.is_dir():
            found += sorted(p for p in path.iterdir()
                            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)
        elif path.is_file():
            found.append(path)
        else:
            print(f"Не найдено, пропускаю: {path}", file=sys.stderr)
    return [p.resolve() for p in found if p.resolve() != banner_path]


def run_batch(paths, banner, times, args):
    videos = find_videos(paths, banner["path"])
    if not videos:
        print(f"Нет видео для обработки. Положите файлы в папку {args.input_dir} "
              f"и запустите ещё раз (или перетащите видео на run.bat).")
        return 0

    done, skipped, failed = 0, [], []
    for video_path in videos:
        if is_done(video_path, args):
            skipped.append(video_path.name)
        elif process_video(video_path, banner, times, args):
            done += 1
        else:
            failed.append(video_path.name)

    print(f"\nИтого: обработано {done}, пропущено как уже готовые {len(skipped)}, "
          f"с ошибками {len(failed)}. Части лежат в {args.output_dir}")
    if skipped:
        print(f"Пропущены (уже есть в output, для повторной обработки добавьте --force): "
              f"{', '.join(skipped)}")
    if failed:
        print(f"Не получилось: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


def run_watch(folder, banner, times, args):
    print(f"\nСлежу за папкой {folder}\n"
          f"Кладите туда видео — части появятся в {args.output_dir}. Для выхода нажмите Ctrl+C.")
    sizes = {}   # путь → размер при прошлой проверке
    failed = {}  # путь → размер файла, на котором была ошибка
    try:
        while True:
            for video_path in find_videos([folder], banner["path"]):
                if is_done(video_path, args):
                    continue
                size = video_path.stat().st_size
                if failed.get(video_path) == size:
                    continue
                if sizes.get(video_path) != size:
                    # Файл ещё копируется — дождёмся, пока размер перестанет меняться.
                    sizes[video_path] = size
                    continue
                sizes.pop(video_path)
                if process_video(video_path, banner, times, args):
                    failed.pop(video_path, None)
                else:
                    failed[video_path] = size
            time.sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        print("\nОстановлено.")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(
        description="Делит видео поминутно и вставляет баннер (ролик со звуком или картинку) "
                    "на 15, 30 и 45 секунде каждой минуты. Без аргументов обрабатывает папку input.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", type=Path,
                        help="видеофайлы или папки (по умолчанию — папка input)")
    parser.add_argument("--banner", type=Path, default=SCRIPT_DIR / "banner.mp4",
                        help="баннер: видео (mp4 со звуком) или картинка (png/jpg)")
    parser.add_argument("--input-dir", type=Path, default=SCRIPT_DIR / "input",
                        help="папка, откуда брать видео, если файлы не указаны")
    parser.add_argument("-o", "--output-dir", type=Path, default=SCRIPT_DIR / "output",
                        help="куда сохранять части (для каждого видео — своя подпапка)")
    parser.add_argument("--watch", action="store_true",
                        help="следить за папкой и обрабатывать новые видео автоматически")
    parser.add_argument("--force", action="store_true",
                        help="обработать заново даже уже готовые видео")
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
    parser.add_argument("--chromakey", default="auto", metavar="auto|off|ЦВЕТ",
                        help="вырезать фон баннера: auto — найти зелёный/синий фон самому, "
                             "off — не вырезать, или цвет, например 0x00FF00")
    parser.add_argument("--key-similarity", type=float, default=0.15,
                        help="насколько близкие к фону цвета тоже вырезать (0.01–1)")
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
    if args.segment <= 0:
        die("--segment должен быть больше нуля")
    try:
        times = sorted(float(t) for t in args.times.split(",") if t.strip())
    except ValueError:
        die(f"неверный формат --times: {args.times!r}, нужно например 15,30,45")
    if not times or any(not 0 <= t < args.segment for t in times):
        die(f"секунды в --times должны быть от 0 до {fmt(args.segment)}")

    args.input_dir = args.input_dir.resolve()
    args.output_dir = args.output_dir.resolve()
    args.input_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    banner = load_banner(args)
    key = f"фон {banner['key_color']} вырезается" if banner["key_color"] else "без вырезания фона"
    print(f"Баннер: {banner['path'].name}, {fmt(banner['duration'])} с, "
          f"{'со звуком' if banner['has_audio'] else 'без звука'}, {key}; "
          f"запуск на {', '.join(fmt(t) for t in times)} с каждой части")
    gaps = [b - a for a, b in zip(times, times[1:])]
    if gaps and banner["duration"] > min(gaps):
        print(f"Внимание: баннер ({fmt(banner['duration'])} с) длиннее промежутка между "
              f"показами ({fmt(min(gaps))} с) — показы будут накладываться друг на друга.")

    if args.watch:
        folders = [p for p in args.inputs if p.is_dir()] or [args.input_dir]
        sys.exit(run_watch(folders[0].resolve(), banner, times, args))
    sys.exit(run_batch(args.inputs or [args.input_dir], banner, times, args))


if __name__ == "__main__":
    main()
