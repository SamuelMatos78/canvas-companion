#!/usr/bin/env python3
"""Build the standalone Canvas Companion executable for the current OS."""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from PyInstaller.__main__ import run as pyinstaller_run
except ImportError as exc:
    raise SystemExit(
        "PyInstaller não encontrado. Instale as dependências com "
        "python -m pip install -r requirements-build.txt."
    ) from exc


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "dist" / "executables"
BUILD = ROOT / ".build" / "pyinstaller" / ("windows" if os.name == "nt" else "linux")


def add_data_args() -> list[str]:
    files = (
        ("App_for_users/index.html", "App_for_users"),
        ("App_for_users/app.js", "App_for_users"),
        ("App_for_users/styles.css", "App_for_users"),
        ("App_for_users/installers/install.cmd", "App_for_users/installers"),
        ("App_for_users/installers/install.ps1", "App_for_users/installers"),
        ("App_for_users/installers/install.sh", "App_for_users/installers"),
        ("canvas_integracao/resumidor/resumidor.py", "canvas_integracao/resumidor"),
        ("exercitor/fazer_exerc.py", "exercitor"),
        ("bibliotecario/reader.py", "bibliotecario"),
        ("bibliotecario/ignore.json", "bibliotecario"),
    )
    args: list[str] = []
    for source, destination in files:
        path = ROOT / source
        if not path.is_file():
            raise SystemExit(f"Arquivo necessário para o pacote não encontrado: {source}")
        args.extend(["--add-data", f"{path}{os.pathsep}{destination}"])
    return args


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    args = [
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "Canvas-Companion",
        "--distpath",
        str(OUTPUT),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(BUILD),
        "--paths",
        str(ROOT),
    ]
    args.extend(add_data_args())
    for package in ("requests", "dotenv", "PIL", "pdf2image", "pytesseract", "pypdf", "reportlab"):
        args.extend(["--collect-all", package])
    args.append(str(ROOT / "puc.py"))
    pyinstaller_run(args)

    suffix = ".exe" if os.name == "nt" else ""
    executable = OUTPUT / f"Canvas-Companion{suffix}"
    if not executable.is_file():
        raise SystemExit("O build terminou sem gerar o executável esperado.")
    print(f"Executável pronto: {executable}")
    print("O primeiro build deve ser feito em cada sistema: Windows para .exe e Linux para o binário Linux.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
