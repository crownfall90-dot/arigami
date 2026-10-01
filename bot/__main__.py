import argparse
import json
from pathlib import Path
from .engine import analyze
from .report import render


def main():
    parser = argparse.ArgumentParser(description='VP-SMC-CVD: анализ предоставленных данных')
    parser.add_argument('input', nargs='?', help='JSON-файл для анализа / исторического воспроизведения')
    parser.add_argument('--json', action='store_true', help='Результат JSON')
    parser.add_argument('--serve', action='store_true', help='Локальный веб-интерфейс и API')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if args.serve:
        from .server import serve
        serve(args.port)
    elif args.input:
        data = json.loads(Path(args.input).read_text(encoding='utf-8-sig'))
        result = analyze(data)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) if args.json else render(result))
    else:
        parser.error('Укажите JSON-файл или --serve')


if __name__ == '__main__':
    main()
