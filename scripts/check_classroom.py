"""Run from the repository root: python scripts/check_classroom.py [--skip-gpu]."""
import argparse
import importlib
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-gpu', action='store_true', help='Only check classroom dependencies and credentials')
    parser.add_argument('--avatar-id', default='wav2lip256_avatar1')
    args = parser.parse_args()
    failures = []
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except ImportError:
        pass
    for name in ['aiohttp', 'aiortc', 'av', 'pymupdf', 'pptx', 'openai', 'edge_tts']:
        try:
            importlib.import_module(name)
            print('OK dependency:', name)
        except ImportError as exc:
            failures.append(f'{name}: {exc}')
    office = shutil.which('libreoffice') or shutil.which('soffice')
    print(('OK LibreOffice: ' + office) if office else 'WARN LibreOffice missing: only PDF import will work')
    secret = os.getenv('LIVETALKING_CLASSROOM_ADMIN_TOKEN', '')
    if len(secret) < 24:
        failures.append('Set LIVETALKING_CLASSROOM_ADMIN_TOKEN to a random secret of at least 24 characters')
    if not (os.getenv('DASHSCOPE_API_KEY') or os.getenv('ORCAROUTER_API_KEY')):
        failures.append('Set the LLM provider API key for classroom questions')
    if not args.skip_gpu:
        try:
            import torch
            if not torch.cuda.is_available():
                failures.append('CUDA is unavailable; install the matching CUDA PyTorch wheel and NVIDIA driver')
            else:
                print('OK CUDA device:', torch.cuda.get_device_name(0))
        except ImportError:
            failures.append('PyTorch is missing')
        for path in [ROOT / 'models/wav2lip.pth', ROOT / 'data/avatars' / args.avatar_id]:
            if not path.exists():
                failures.append('Missing wav2lip model/avatar: ' + str(path))
    for failure in failures:
        print('FAIL:', failure)
    print('This checks prerequisites, not live GPU inference, TTS/LLM credentials or network reachability.')
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
