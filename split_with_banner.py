#!/usr/bin/env python3
"""
Делит видео на части по одной минуте, переводит их в вертикальный формат
TikTok (1080×1920) и в каждой части показывает баннер на 15-й, 30-й и 45-й
секунде.

Когда начинается баннер, видео замирает на текущем кадре (звук видео тоже
делает паузу), поверх играет баннер со своим звуком, а после него видео
продолжается с того же места. Если у баннера зелёный (или синий) фон, он
определяется и вырезается автоматически.

Обычная работа:
    1. Положить видео в папку input
    2. Запустить:  python split_with_banner.py   (на Windows — run.bat)
    3. Забрать части из папки output/<имя видео>/

Ещё примеры:
    python split_with_banner.py video1.mp4 video2.mp4      # конкретные файлы
    python split_with_banner.py --watch                     # следить за папкой input
    python split_with_banner.py --fit crop                  # заполнить экран, обрезав края
    python split_with_banner.py --no-freeze                 # баннер поверх идущего видео
    python split_with_banner.py --fit original              # оставить исходный размер
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
# Короткое затухание звука на стыках с заморозкой, чтобы не было щелчков.
FADE = 0.03

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


def fit_filter(args, canvas):
    """Подгоняет кадр под формат TikTok (или оставляет как есть)."""
    if args.fit == "original":
        return "null"
    w, h = canvas
    scale = f"scale={w}:{h}:force_original_aspect_ratio="
    if args.fit == "crop":
        return f"{scale}increase,crop={w}:{h},setsar=1"
    if args.fit == "pad":
        return (f"{scale}decrease:force_divisible_by=2,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
    # blur: видео целиком по центру, вокруг — размытая увеличенная копия кадра.
    # Размываем уменьшенную копию: так в разы быстрее, а на вид то же самое.
    sw, sh = w // 8 * 2, h // 8 * 2
    return (f"split[bg][fg];"
            f"[bg]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
            f"boxblur=10:2,scale={w}:{h}[bgblur];"
            f"[fg]{scale}decrease:force_divisible_by=2[fgfit];"
            f"[bgblur][fgfit]overlay=(W-w)/2:(H-h)/2,setsar=1")


def build_part(args, video_path, video, banner, canvas, start, part_len, times):
    """Собирает входы ffmpeg и filter_complex для одной части.

    Возвращает (входы, фильтр, длительность готовой части).
    """
    duration = banner["duration"]
    has_audio = video["has_audio"]
    inputs, graph = [], []

    if args.freeze and times:
        # Режем кусок исходника на отрезки по секундам баннера. После каждого
        # отрезка, кроме последнего, держим последний кадр, пока идёт баннер,
        # а звук видео на это время заменяем тишиной.
        bounds = [0, *times, part_len]
        pieces = []
        for k, (a, b) in enumerate(zip(bounds, bounds[1:])):
            inputs += ["-ss", fmt(start + a), "-t", fmt(b - a), "-i", str(video_path)]
            last = k == len(bounds) - 2
            chain = f"[{k}:v]setpts=PTS-STARTPTS"
            if not last:
                chain += f",tpad=stop_mode=clone:stop_duration={fmt(duration)}"
            graph.append(f"{chain}[mv{k}]")
            pieces.append(f"[mv{k}]")
            if has_audio:
                fade = min(FADE, (b - a) / 2)
                chain = f"[{k}:a]asetpts=PTS-STARTPTS"
                if k > 0:
                    chain += f",afade=t=in:d={fmt(fade)}"
                if not last:
                    chain += (f",afade=t=out:st={fmt(b - a - fade)}:d={fmt(fade)}"
                              f",apad=pad_dur={fmt(duration)}")
                graph.append(f"{chain}[ma{k}]")
                pieces.append(f"[ma{k}]")
        main_inputs = len(bounds) - 1
        outputs = "[mv][ma]" if has_audio else "[mv]"
        graph.append(f"{''.join(pieces)}concat=n={main_inputs}:v=1:a={int(has_audio)}{outputs}")
        # Каждая заморозка сдвигает следующие показы на длину баннера.
        starts = [t + k * duration for k, t in enumerate(times)]
        out_len = part_len + len(times) * duration
    else:
        inputs += ["-ss", fmt(start), "-t", fmt(part_len), "-i", str(video_path)]
        main_inputs = 1
        graph.append("[0:v]null[mv]")
        if has_audio:
            graph.append("[0:a]anull[ma]")
        starts = times
        out_len = part_len

    graph.append(f"[mv]{fit_filter(args, canvas)}[base]")

    cw, ch = canvas
    margin = round(ch * args.margin)
    x_tpl, y_tpl = POSITIONS[args.position]
    x, y = x_tpl.format(m=margin), y_tpl.format(m=margin)
    if args.position == "full":
        size = f"scale={cw}:{ch}:force_original_aspect_ratio=decrease"
    elif args.width:
        size = f"scale={round(cw * args.width / 2) * 2}:-2"
    else:
        # Исходный размер, но не больше кадра.
        size = f"scale='min(iw,{cw})':'min(ih,{ch})':force_original_aspect_ratio=decrease"

    last_video = "base"
    for i, t in enumerate(starts):
        index = main_inputs + i
        inputs += banner["input"]
        chain = f"[{index}:v]"
        if banner["key_color"]:
            chain += f"chromakey={banner['key_color']}:{args.key_similarity}:0.05,"
        # Сдвигаем начало ролика на нужную секунду части.
        graph.append(f"{chain}{size},setpts=PTS-STARTPTS+{fmt(t)}/TB[b{i}]")
        graph.append(f"[{last_video}][b{i}]overlay=x={x}:y={y}:eof_action=pass[v{i}]")
        last_video = f"v{i}"
    graph.append(f"[{last_video}]format=yuv420p[v]")

    if has_audio:
        main_audio = "[ma]"
        if starts and args.main_volume != 1 and not args.freeze:
            shown = "+".join(f"between(t,{fmt(t)},{fmt(t + duration)})" for t in starts)
            main_audio += f"volume=volume={args.main_volume}:enable='{shown}',"
        graph.append(f"{main_audio}anull[a0]")
    else:
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={fmt(out_len)}[a0]")

    mix = ["[a0]"]
    if banner["has_audio"]:
        for i, t in enumerate(starts):
            graph.append(f"[{main_inputs + i}:a]asetpts=PTS-STARTPTS,volume={args.banner_volume},"
                         f"adelay={round(t * 1000)}:all=1[ba{i}]")
            mix.append(f"[ba{i}]")
    if len(mix) > 1:
        graph.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0[a]")
    else:
        graph.append("[a0]anull[a]")

    return inputs, ";".join(graph), out_len


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

    canvas = (video["width"], video["height"]) if args.fit == "original" else args.canvas
    print(f"\n=== {video_path.name}: {video['width']}x{video['height']}, "
          f"{fmt(total)} с → частей: {parts}, кадр {canvas[0]}x{canvas[1]}")
    print(f"Сохраняю в: {out_dir}")

    for index in range(parts):
        start = index * args.segment
        part_len = min(args.segment, total - start)
        # В последней, короткой части баннер запускаем, только если он успевает начаться.
        part_times = [t for t in times if t < part_len - 0.1]
        output = out_dir / f"{video_path.stem}_part_{index + 1:03d}.mp4"

        inputs, graph, out_len = build_part(args, video_path, video, banner, canvas,
                                            start, part_len, part_times)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
            *inputs,
            "-filter_complex", graph,
            "-map", "[v]", "-map", "[a]",
            "-t", fmt(out_len),
            "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(output),
        ]

        print(f"[{index + 1}/{parts}] {output.name} "
              f"(исходник {fmt(start)}–{fmt(start + part_len)} с → {fmt(out_len)} с)")
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
        description="Делит видео поминутно, переводит в вертикальный формат TikTok и вставляет "
                    "баннер на 15, 30 и 45 секунде каждой минуты, замораживая видео на время "
                    "баннера. Без аргументов обрабатывает папку input.",
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
                        help="длина одной части в секундах (по исходному видео)")
    parser.add_argument("--fit", choices=["blur", "crop", "pad", "original"], default="blur",
                        help="как подогнать видео под вертикальный экран: blur — целиком по центру "
                             "на размытом фоне, crop — заполнить экран, обрезав края, "
                             "pad — чёрные полосы, original — оставить исходный размер")
    parser.add_argument("--size", default="1080x1920",
                        help="размер кадра готового видео (ширина x высота)")
    parser.add_argument("--no-freeze", dest="freeze", action="store_false",
                        help="не замораживать видео: баннер идёт поверх движущегося видео")
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
                        help="громкость основного видео под баннером (только с --no-freeze)")
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
        times = sorted({float(t) for t in args.times.split(",") if t.strip()})
    except ValueError:
        die(f"неверный формат --times: {args.times!r}, нужно например 15,30,45")
    if not times or any(not 0 <= t < args.segment for t in times):
        die(f"секунды в --times должны быть от 0 до {fmt(args.segment)}")
    try:
        width, height = (int(n) for n in args.size.lower().split("x"))
    except ValueError:
        die(f"неверный формат --size: {args.size!r}, нужно например 1080x1920")
    if width < 16 or height < 16:
        die("--size слишком маленький")
    # Кодеку нужны чётные размеры.
    args.canvas = (width // 2 * 2, height // 2 * 2)

    args.input_dir = args.input_dir.resolve()
    args.output_dir = args.output_dir.resolve()
    args.input_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    banner = load_banner(args)
    key = f"фон {banner['key_color']} вырезается" if banner["key_color"] else "без вырезания фона"
    print(f"Баннер: {banner['path'].name}, {fmt(banner['duration'])} с, "
          f"{'со звуком' if banner['has_audio'] else 'без звука'}, {key}; "
          f"запуск на {', '.join(fmt(t) for t in times)} с каждой части")
    fit = {"blur": "по центру на размытом фоне", "crop": "на весь экран с обрезкой краёв",
           "pad": "с чёрными полосами", "original": "исходный размер"}[args.fit]
    size = "" if args.fit == "original" else f"{args.canvas[0]}x{args.canvas[1]}, "
    freeze = ("видео замирает на время баннера" if args.freeze
              else "баннер идёт поверх движущегося видео")
    print(f"Формат: {size}{fit}; {freeze}")
    gaps = [b - a for a, b in zip(times, times[1:])]
    # С заморозкой показы не накладываются: видео стоит, пока идёт баннер.
    if not args.freeze and gaps and banner["duration"] > min(gaps):
        print(f"Внимание: баннер ({fmt(banner['duration'])} с) длиннее промежутка между "
              f"показами ({fmt(min(gaps))} с) — показы будут накладываться друг на друга.")

    if args.watch:
        folders = [p for p in args.inputs if p.is_dir()] or [args.input_dir]
        sys.exit(run_watch(folders[0].resolve(), banner, times, args))
    sys.exit(run_batch(args.inputs or [args.input_dir], banner, times, args))


if __name__ == "__main__":
    main()
