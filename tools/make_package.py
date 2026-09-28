"""打包給別人用：產生 dist/ddr-pose_YYYYMMDD.zip。

只放程式與說明（約數百 KB）；不含 data/（分析結果）、.venv/（對方安裝時自己建）、模型（安裝時自動下載）。
用法：python tools/make_package.py（或雙擊 make_package.bat）
"""
from __future__ import annotations

import datetime
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = ['server.py', 'requirements.txt', 'setup.bat', 'start.bat', 'README.md',
           'はじめにお読みください.txt', 'ddrpose', 'web', 'tools/setup.ps1']


def main():
    sys.stdout.reconfigure(errors='replace')
    out = ROOT / 'dist' / f'ddr-pose_{datetime.date.today():%Y%m%d}.zip'
    out.parent.mkdir(exist_ok=True)
    n = 0
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for item in INCLUDE:
            p = ROOT / item
            if not p.exists():
                raise SystemExit(f'缺少檔案：{p}')
            files = [p] if p.is_file() else sorted(f for f in p.rglob('*') if f.is_file() and '__pycache__' not in f.parts)
            for f in files:
                z.write(f, Path('ddr-pose') / f.relative_to(ROOT))
                n += 1
    print(f'{out}（{n} 個檔案，{out.stat().st_size / 1024:.0f} KB）')


if __name__ == '__main__':
    main()
