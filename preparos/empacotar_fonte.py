#!/usr/bin/env python3
"""Cria um ZIP enxuto e sem dados pessoais para publicar no Google Drive."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlparse
import zipfile


ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "README.md",
    "puc.py",
    "requirements.txt",
    "requirements-build.txt",
    "preparos/build_executable.py",
    "preparos/empacotar_fonte.py",
    "puc_tools/__init__.py",
    "puc_tools/openai_compat.py",
    "puc_tools/paths.py",
    "dashboard/api.py",
    "canvas_integracao/resumidor/resumidor.py",
    "canvas_integracao/resumidor/res.env.example",
    "exercitor/fazer_exerc.py",
    "exercitor/.env.example",
    "bibliotecario/reader.py",
    "bibliotecario/ignore.json",
    "bibliotecario/.env.example",
)
APP_FILES = (
    "index.html",
    "app.js",
    "styles.css",
    "README.md",
    "installers/install.cmd",
    "installers/install.ps1",
    "installers/install.sh",
)


def add_file(archive: zipfile.ZipFile, source: Path, arcname: str) -> None:
    if not source.is_file():
        raise SystemExit(f"Arquivo necessário ausente: {source.relative_to(ROOT)}")
    archive.write(source, arcname)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Empacota apenas o código e os instaladores, sem cache do Canvas, tokens ou ambientes virtuais."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "canvas-companion-source.zip",
        help="Destino do ZIP (padrão: dist/canvas-companion-source.zip).",
    )
    parser.add_argument(
        "--source-url",
        help="Link HTTPS público do ZIP no Google Drive, embutido nos instaladores gerados.",
    )
    args = parser.parse_args()
    output = args.output.expanduser()
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)

    source_url = args.source_url or ""
    if source_url:
        parsed_url = urlparse(source_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "drive.google.com" or "\n" in source_url or "\r" in source_url:
            raise SystemExit("Informe o link HTTPS público do ZIP no Google Drive.")

    windows_script = (ROOT / "App_for_users" / "installers" / "install.ps1").read_text(encoding="utf-8")
    linux_script = (ROOT / "App_for_users" / "installers" / "install.sh").read_text(encoding="utf-8")
    if source_url:
        windows_marker = '$DefaultSourceUrl = ""'
        linux_marker = 'DEFAULT_SOURCE_URL=""'
        if windows_marker not in windows_script or linux_marker not in linux_script:
            raise SystemExit("Não encontrei os campos DEFAULT_SOURCE_URL nos instaladores.")
        windows_script = windows_script.replace(
            windows_marker,
            "$DefaultSourceUrl = '" + source_url.replace("'", "''") + "'",
            1,
        )
        linux_quoted_url = "'" + source_url.replace("'", "'\"'\"'") + "'"
        linux_script = linux_script.replace(linux_marker, "DEFAULT_SOURCE_URL=" + linux_quoted_url, 1)

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as archive:
        for relative in FILES:
            add_file(archive, ROOT / relative, f"canvas-companion/{relative}")
        for relative in APP_FILES:
            add_file(archive, ROOT / "App_for_users" / relative, f"canvas-companion/App_for_users/{relative}")

    windows_bundle = output.parent / "canvas-companion-windows-installer.zip"
    with zipfile.ZipFile(windows_bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as archive:
        archive.write(ROOT / "App_for_users" / "installers" / "install.cmd", "install.cmd")
        archive.writestr("install.ps1", windows_script)

    linux_installer = output.parent / "canvas-companion-linux-install.sh"
    linux_installer.write_text(linux_script, encoding="utf-8", newline="\n")
    linux_installer.chmod(0o755)

    print(f"ZIP pronto: {output}")
    print(f"Instalador Windows pronto: {windows_bundle}")
    print(f"Instalador Linux pronto: {linux_installer}")
    print("Conteúdo-fonte: código necessário para execução + interface + instaladores.")
    print("Dados do Canvas, chaves, caches, ambientes virtuais e dependências web não são incluídos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
