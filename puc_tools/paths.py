"""Local unico dos dados sincronizados do Canvas Companion.

O executavel e a versao em script compartilham o mesmo ``config.json`` do
usuario; por isso tambem precisam compartilhar a mesma biblioteca. Antes, cada
modo gravava os downloads em uma pasta diferente.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_DATA_ROOT = PROJECT_ROOT / "canvas_integracao" / "resumidor"


def user_data_root() -> Path:
    """Pasta de dados do usuario, igual para o executavel e para os scripts."""
    configured = os.environ.get("PUC_DATA_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        preferred = base / "PUC Central" / "data"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        preferred = base / "puc-central" / "data"
    legacy_downloads = LEGACY_DATA_ROOT / "downloads_canvas"
    # Instalacoes antigas em script guardavam tudo dentro do repositorio. Elas
    # continuam funcionando ate o usuario mover os dados para a pasta comum.
    if not (preferred / "downloads_canvas").is_dir() and legacy_downloads.is_dir():
        return LEGACY_DATA_ROOT.resolve()
    return preferred.resolve()


def downloads_root() -> Path:
    return Path(os.environ.get(
        "PUC_DOWNLOADS_ROOT",
        str(user_data_root() / "downloads_canvas"),
    )).expanduser().resolve()


def courses_cache_path() -> Path:
    return Path(os.environ.get(
        "PUC_COURSES_CACHE",
        str(user_data_root() / "cursos.json"),
    )).expanduser().resolve()


def jobs_root() -> Path:
    return user_data_root() / "execucoes"
