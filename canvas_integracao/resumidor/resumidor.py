#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from puc_tools import paths as data_paths  # noqa: E402


DEFAULT_CANVAS_BASE_URL = "https://pucminas.instructure.com"
DEFAULT_TIMEOUT_SECONDS = 60


class CanvasAPIError(RuntimeError):
    """Erro seguro e contextualizado ao consultar a API do Canvas."""


@dataclass(frozen=True)
class AssignmentTarget:
    course_id: str
    assignment_id: Optional[str] = None

    @property
    def api_path(self) -> str:
        path = f"/api/v1/courses/{self.course_id}/assignments"
        return f"{path}/{self.assignment_id}" if self.assignment_id else path


@dataclass(frozen=True)
class DownloadResult:
    success: bool
    error: Optional[str] = None


@dataclass
class CourseSelection:
    course_id: str
    assignment_ids: Optional[set[str]]
    assignments_only: bool


# =========================
# URLs e HTTP
# =========================
def _origin(url: str) -> Tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), parsed.netloc.lower()


def normalize_canvas_base_url(value: Optional[str]) -> str:
    base = (value or DEFAULT_CANVAS_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("CANVAS_BASE_URL deve ser uma URL HTTP(S) absoluta.")
    if parsed.query or parsed.fragment:
        raise ValueError("CANVAS_BASE_URL não pode conter query ou fragmento.")
    return base


def _safe_url(url: str) -> str:
    """Remove query/fragment para não vazar URLs assinadas em mensagens."""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _validate_same_origin(url: str, trusted_url: str, context: str) -> None:
    if _origin(url) != _origin(trusted_url):
        raise CanvasAPIError(
            f"{context} recusado por apontar para outra origem: {_safe_url(url)}"
        )


def parse_canvas_assignment_url(
    ui_url: str,
    canvas_base_url: str = DEFAULT_CANVAS_BASE_URL,
) -> AssignmentTarget:
    """Converte semanticamente uma URL da UI de assignments em IDs da API."""
    parsed = urlsplit((ui_url or "").strip())
    base = urlsplit(canvas_base_url.rstrip("/"))

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("A URL do Canvas deve ser absoluta e usar HTTP(S).")
    if _origin(ui_url) != _origin(canvas_base_url):
        raise ValueError("A URL informada não pertence à origem Canvas configurada.")

    match = re.fullmatch(
        r"/courses/(?P<course_id>\d+)/assignments(?:/(?P<assignment_id>\d+))?/?",
        parsed.path,
    )
    if not match:
        raise ValueError(
            "URL de atividade inválida; esperado /courses/{id}/assignments[/] "
            "ou /courses/{id}/assignments/{assignment_id}."
        )

    return AssignmentTarget(
        course_id=match.group("course_id"),
        assignment_id=match.group("assignment_id"),
    )


def canvas_ui_url_to_api_url(
    ui_url: str,
    canvas_base_url: str = DEFAULT_CANVAS_BASE_URL,
) -> str:
    target = parse_canvas_assignment_url(ui_url, canvas_base_url)
    return f"{canvas_base_url.rstrip('/')}{target.api_path}"


def convert_canvas_url_to_api(
    ui_url: str,
    canvas_base_url: str = DEFAULT_CANVAS_BASE_URL,
) -> str:
    """Alias descritivo mantido para consumidores externos."""
    return canvas_ui_url_to_api_url(ui_url, canvas_base_url)


def _url_with_query(url: str, params: Iterable[Tuple[str, Any]]) -> str:
    query = urlencode(list(params), doseq=True)
    return f"{url}?{query}" if query else url


def parse_next_link(link_header: Optional[str]) -> Optional[str]:
    if not link_header:
        return None

    for match in re.finditer(
        r'<([^>]+)>\s*;\s*rel\s*=\s*"?([^",;]+)"?',
        str(link_header),
        flags=re.IGNORECASE,
    ):
        relations = {part.lower() for part in match.group(2).split()}
        if "next" in relations:
            return match.group(1)
    return None


def _get_json_response(
    session: requests.Session,
    url: str,
    *,
    trusted_origin: str,
    expected_type: type,
    allow_statuses: Sequence[int] = (),
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Tuple[requests.Response, Any]:
    _validate_same_origin(url, trusted_origin, "Endpoint autenticado")

    try:
        response = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise CanvasAPIError(
            f"Falha de rede ao consultar {_safe_url(url)} ({type(exc).__name__})."
        ) from exc

    if response.status_code in allow_statuses:
        return response, None
    if response.status_code < 200 or response.status_code >= 300:
        raise CanvasAPIError(
            f"Canvas respondeu HTTP {response.status_code} em {_safe_url(url)}."
        )

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise CanvasAPIError(
            f"Canvas retornou JSON inválido em {_safe_url(url)}."
        ) from exc

    if not isinstance(data, expected_type):
        raise CanvasAPIError(
            f"Resposta inesperada em {_safe_url(url)}: esperado "
            f"{expected_type.__name__}, recebido {type(data).__name__}."
        )
    return response, data


def get_json_object(
    session: requests.Session,
    url: str,
    *,
    trusted_origin: str,
    allow_statuses: Sequence[int] = (),
) -> Optional[dict]:
    _, data = _get_json_response(
        session,
        url,
        trusted_origin=trusted_origin,
        expected_type=dict,
        allow_statuses=allow_statuses,
    )
    return data


def get_paginated(
    session: requests.Session,
    url: str,
    *,
    trusted_origin: Optional[str] = None,
    max_pages: int = 1000,
) -> List[dict]:
    """Segue o Link rel=next como URL opaca, sempre restringindo a mesma origem."""
    trusted_origin = trusted_origin or url
    results: List[dict] = []
    next_url: Optional[str] = url
    seen: set[str] = set()
    pages = 0

    while next_url:
        if next_url in seen:
            raise CanvasAPIError("Loop detectado na paginação do Canvas.")
        if pages >= max_pages:
            raise CanvasAPIError(f"Paginação excedeu o limite de {max_pages} páginas.")

        seen.add(next_url)
        pages += 1
        response, data = _get_json_response(
            session,
            next_url,
            trusted_origin=trusted_origin,
            expected_type=list,
        )
        results.extend(data)

        link_header = response.headers.get("Link")
        candidate = parse_next_link(link_header)
        if candidate:
            _validate_same_origin(candidate, trusted_origin, "Link de paginação")
        next_url = candidate

    return results


# =========================
# Utils (gerais)
# =========================
def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", str(name)).strip().rstrip(". ")
    return name or "arquivo"


def canvas_file_local_name(file_id: Any, display_name: Any) -> str:
    """Mantém nomes legíveis sem permitir colisão entre IDs distintos."""
    try:
        stable_id = str(int(file_id))
    except (TypeError, ValueError) as exc:
        raise CanvasAPIError("Arquivo retornado sem um id numérico válido.") from exc
    return f"{stable_id}_{safe_filename(str(display_name))}"


class LocalFileIndex:
    """Arquivos do curso já presentes no disco, indexados pelo id do Canvas.

    O mesmo arquivo costuma aparecer em /files, em um módulo e em várias
    atividades. Uma cópia local evita baixá-lo de novo a cada pasta.
    """

    def __init__(self, course_dir: Path) -> None:
        self._by_id: Dict[str, List[Path]] = {}
        if not course_dir.is_dir():
            return
        for current, dirs, files in os.walk(course_dir):
            dirs[:] = [name for name in dirs if name not in {"ambiente_pratica", "resolucao_ia", "resumos"}]
            for name in files:
                match = re.match(r"(\d+)_", name)
                if match and not name.endswith(".part"):
                    self._by_id.setdefault(match.group(1), []).append(Path(current) / name)

    def add(self, file_id: Any, path: Path) -> None:
        self._by_id.setdefault(str(file_id), []).append(path)

    def copy_to(self, file_id: Any, dest: Path, expected_size: Optional[int]) -> bool:
        for source in self._by_id.get(str(file_id), []):
            if source == dest or not already_downloaded(source, expected_size):
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
            except OSError:
                continue
            self.add(file_id, dest)
            return True
        return False


def fetch_or_copy_file(
    session: requests.Session,
    download_session: requests.Session,
    base: str,
    file_id: Any,
    dest_dir: Path,
    fallback_name: Any,
    local_index: Optional[LocalFileIndex],
) -> Tuple[str, Optional[Path], Optional[DownloadResult]]:
    """Retorna ("skip"|"copy"|"download", destino, resultado do download)."""
    meta = fetch_file_download_url(session, base, int(file_id))
    display_name = meta.get("display_name") or meta.get("filename") or fallback_name or f"file_{file_id}"
    dest = dest_dir / canvas_file_local_name(file_id, display_name)
    if already_downloaded(dest, meta.get("size")):
        return "skip", dest, None
    if local_index is not None and local_index.copy_to(file_id, dest, meta.get("size")):
        return "copy", dest, None
    download_url = meta.get("url")
    if not isinstance(download_url, str) or not download_url:
        return "download", dest, DownloadResult(False, "URL de download ausente")
    result = download_file(download_session, download_url, dest, expected_size=meta.get("size"))
    if result.success and local_index is not None:
        local_index.add(file_id, dest)
    return "download", dest, result


def normalize_text_key(s: str) -> str:
    s = (s or "").strip().upper()
    s = (
        s.replace("Á", "A")
        .replace("À", "A")
        .replace("Â", "A")
        .replace("Ã", "A")
        .replace("É", "E")
        .replace("Ê", "E")
        .replace("Í", "I")
        .replace("Ó", "O")
        .replace("Ô", "O")
        .replace("Õ", "O")
        .replace("Ú", "U")
        .replace("Ç", "C")
    )
    s = re.sub(r"\s+", " ", s).strip()
    return safe_filename(s)


def already_downloaded(path: Path, expected_size: Optional[int]) -> bool:
    if not path.exists():
        return False
    if expected_size is not None:
        try:
            return path.stat().st_size == int(expected_size)
        except (OSError, TypeError, ValueError):
            return False
    return True


def create_download_session() -> requests.Session:
    """Sessão sem Bearer; URLs de arquivo do Canvas já são assinadas."""
    session = requests.Session()
    session.headers.update({"Accept": "*/*"})
    session.headers.pop("Authorization", None)
    return session


def download_file(
    download_session: requests.Session,
    url: str,
    dest: Path,
    expected_size: Optional[int] = None,
) -> DownloadResult:
    """Baixa com sessão anônima e só confirma sucesso após troca atômica."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(dest.suffix + ".part")

    try:
        with download_session.get(url, stream=True, timeout=120) as response:
            if response.status_code < 200 or response.status_code >= 300:
                return DownloadResult(False, f"HTTP {response.status_code}")

            with temp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        handle.write(chunk)

        if expected_size is not None:
            try:
                wanted = int(expected_size)
            except (TypeError, ValueError):
                return DownloadResult(False, "tamanho esperado inválido")
            actual = temp.stat().st_size
            if actual != wanted:
                return DownloadResult(
                    False,
                    f"tamanho incompleto ({actual}/{wanted} bytes)",
                )

        temp.replace(dest)
        return DownloadResult(True)
    except (requests.RequestException, OSError) as exc:
        return DownloadResult(False, type(exc).__name__)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _print_download_result(label: str, result: DownloadResult) -> None:
    if result.success:
        print(f"    [OK] {label}")
    else:
        print(f"    [ERRO DOWNLOAD] {label} ({result.error or 'erro desconhecido'})")


def _track_download_result(
    label: str,
    result: DownloadResult,
    failures: Optional[List[str]],
) -> None:
    """Exibe o resultado e registra falhas para definir o exit code do CLI."""
    _print_download_result(label, result)
    if failures is not None and not result.success:
        failures.append(f"{label}: {result.error or 'erro desconhecido'}")


def _report_file_outcome(
    label: str,
    outcome: str,
    dest: Optional[Path],
    result: Optional[DownloadResult],
    failures: Optional[List[str]],
    prefix: str = "",
) -> None:
    name = re.sub(r"^\d+_", "", dest.name) if dest is not None else label
    shown = f"{prefix}{name}"
    if outcome == "skip":
        print(f"    [SKIP] {shown}")
    elif outcome == "copy":
        print(f"    [COPIA] {shown} (já existia em outra pasta do curso)")
    elif result is not None:
        _track_download_result(shown if result.success else label, result, failures)


def _track_file_exception(
    label: str,
    exc: Exception,
    failures: Optional[List[str]],
) -> None:
    """Notifica uma falha isolada sem interromper os próximos arquivos."""
    message = str(exc).strip() or type(exc).__name__
    _track_download_result(label, DownloadResult(False, message), failures)


# =========================
# Cursos: normalização + cache cursos.json
# =========================
def normalize_course_key(course: Mapping[str, Any]) -> str:
    raw = str(course.get("course_code") or course.get("name") or "")
    raw = re.sub(r"^\(\d+\)\s*", "", raw).strip()
    first = re.split(r"\s*[-–]\s*", raw, maxsplit=1)[0].strip()
    tokens = first.split()
    base = tokens[0] if tokens else first
    base = normalize_text_key(base)
    return base or "CURSO"


def build_courses_dict(
    courses: List[dict],
    previous: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Gera chaves de pasta estáveis entre sincronizações.

    A ordem devolvida pelo Canvas pode mudar; sem estabilidade, TOPICOS e
    TOPICOS_2 trocariam de curso e misturariam os arquivos das matérias.
    """
    known = {str(course_id): str(key) for key, course_id in (previous or {}).items()}
    valid = [course for course in courses if course.get("id") is not None]
    out: Dict[str, str] = {}
    pending: List[dict] = []

    for course in valid:
        course_id = str(course.get("id"))
        key = known.get(course_id)
        if key and key not in out:
            out[key] = course_id
        else:
            pending.append(course)

    def numeric_id(course: dict) -> Tuple[int, str]:
        raw = str(course.get("id"))
        return (int(raw), raw) if raw.isdigit() else (0, raw)

    for course in sorted(pending, key=numeric_id):
        base_key = normalize_course_key(course)
        key = base_key
        suffix = 1
        while key in out or key in known.values():
            suffix += 1
            key = f"{base_key}_{suffix}"
        out[key] = str(course.get("id"))

    return dict(sorted(out.items()))


def load_or_fetch_courses(
    session: requests.Session,
    base: str,
    json_path: Path,
    *,
    refresh: bool = False,
) -> Dict[str, str]:
    cached: Dict[str, str] = {}
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            if not refresh:
                raise CanvasAPIError(f"Não foi possível ler {json_path.name}: {exc}") from exc
            data = {}
        if not isinstance(data, dict):
            if not refresh:
                raise CanvasAPIError(f"{json_path.name} existe, mas não é um objeto JSON.")
            data = {}
        cached = {str(key): str(value) for key, value in data.items()}
        if not refresh:
            return cached

    url = _url_with_query(
        f"{base}/api/v1/courses",
        (("enrollment_state", "active"), ("per_page", 100)),
    )
    courses = get_paginated(session, url, trusted_origin=base)
    cursos_id = build_courses_dict(courses, cached)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(cursos_id, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[OK] {json_path.name} atualizado ({len(cursos_id)} cursos).")
    return cursos_id


# =========================
# Arquivos e módulos
# =========================
def fetch_file_download_url(
    session: requests.Session,
    base: str,
    file_id: int,
) -> dict:
    url = f"{base}/api/v1/files/{int(file_id)}"
    data = get_json_object(session, url, trusted_origin=base)
    assert data is not None
    return data


def download_modules_items(
    session: requests.Session,
    base: str,
    course_id: str,
    course_dir: Path,
    *,
    download_session: Optional[requests.Session] = None,
    metadata_only: bool = False,
    download_failures: Optional[List[str]] = None,
    local_index: Optional[LocalFileIndex] = None,
) -> None:
    download_session = download_session or create_download_session()
    modules_dir = course_dir / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)

    modules_url = _url_with_query(
        f"{base}/api/v1/courses/{course_id}/modules",
        (("per_page", 100),),
    )
    modules = get_paginated(session, modules_url, trusted_origin=base)
    if not modules:
        print("  [INFO] sem módulos disponíveis.")
        return

    for module in modules:
        module_name = module.get("name") or f"module_{module.get('id')}"
        module_folder = modules_dir / normalize_text_key(str(module_name))
        module_folder.mkdir(parents=True, exist_ok=True)
        (module_folder / "_module.json").write_text(
            json.dumps(module, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        items_url = module.get("items_url")
        if not isinstance(items_url, str) or not items_url:
            print(f"  [INFO] módulo sem items_url: {module_name}")
            continue
        _validate_same_origin(items_url, base, "items_url de módulo")
        if "per_page=" not in items_url:
            items_url = f"{items_url}{'&' if '?' in items_url else '?'}per_page=100"
        items = get_paginated(session, items_url, trusted_origin=base)
        (module_folder / "_items.json").write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  Módulo: {module_name} ({len(items)} itens)")

        if metadata_only:
            continue
        for item in items:
            if item.get("type") != "File":
                continue
            file_id = item.get("content_id")
            if file_id is None:
                _track_download_result(
                    f"módulo {module_name} -> arquivo sem id",
                    DownloadResult(False, "content_id ausente"),
                    download_failures,
                )
                continue
            label = f"módulo {module_name} -> arquivo id={file_id}"
            try:
                outcome, dest, result = fetch_or_copy_file(
                    session, download_session, base, file_id,
                    module_folder, item.get("title"), local_index,
                )
                _report_file_outcome(label, outcome, dest, result, download_failures)
            except Exception as exc:
                _track_file_exception(label, exc, download_failures)
                continue


def extract_canvas_file_ids_from_html(html_text: str) -> List[int]:
    ids: set[int] = set()
    patterns = (
        r'data-api-endpoint=["\']https?://[^"\']*/api/v1/courses/\d+/files/(\d+)',
        r'href=["\']https?://[^"\']*/courses/\d+/files/(\d+)[^"\']*["\']',
        r'href=["\']/courses/\d+/files/(\d+)[^"\']*["\']',
    )
    for pattern in patterns:
        for match in re.findall(pattern, html_text or "", flags=re.IGNORECASE):
            try:
                ids.add(int(match))
            except (TypeError, ValueError):
                continue
    return sorted(ids)


def extract_links_from_html(html_text: str) -> List[str]:
    links = []
    for href in re.findall(
        r'href=["\']([^"\']+)["\']', html_text or "", flags=re.IGNORECASE
    ):
        href = unescape(href).strip()
        if href:
            links.append(href)
    return sorted(set(links))


def build_assignment_folder_name(assignment: Mapping[str, Any]) -> str:
    assignment_id = assignment.get("id", "sem_id")
    name = assignment.get("name") or f"assignment_{assignment_id}"
    try:
        position_label = f"{int(assignment.get('position')):03d}"
    except (TypeError, ValueError):
        position_label = "000"
    return safe_filename(f"{position_label}_{name}_{assignment_id}")


def resolve_assignment_folder(exercises_dir: Path, assignment: Mapping[str, Any]) -> Path:
    """Reaproveita a pasta já existente da atividade, identificada pelo id.

    O nome da pasta inclui posição e título; quando o professor reordena ou
    renomeia a atividade, criar outra pasta duplicaria a atividade e deixaria o
    ambiente de prática e a resposta da IA na pasta antiga.
    """
    wanted = exercises_dir / build_assignment_folder_name(assignment)
    if wanted.is_dir():
        return wanted
    assignment_id = str(assignment.get("id") or "")
    if assignment_id and exercises_dir.is_dir():
        suffix = f"_{assignment_id}"
        for candidate in sorted(exercises_dir.iterdir()):
            if not candidate.is_dir() or not candidate.name.endswith(suffix):
                continue
            try:
                stored = json.loads((candidate / "assignment.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(stored, dict) and str(stored.get("id")) == assignment_id:
                return candidate
    return wanted


def fetch_current_submission(
    session: requests.Session,
    base: str,
    course_id: str,
    assignment_id: str,
) -> Optional[dict]:
    url = _url_with_query(
        f"{base}/api/v1/courses/{course_id}/assignments/"
        f"{assignment_id}/submissions/self",
        (("include[]", "submission_history"),),
    )
    return get_json_object(
        session,
        url,
        trusted_origin=base,
        allow_statuses=(404,),
    )


def fetch_assignment_bundle(
    session: requests.Session,
    base: str,
    course_id: str,
    assignment_id: str,
) -> Tuple[dict, Optional[dict]]:
    detail_url = _url_with_query(
        f"{base}/api/v1/courses/{course_id}/assignments/{assignment_id}",
        (("include[]", "submission"), ("include[]", "can_submit")),
    )
    detail = get_json_object(session, detail_url, trusted_origin=base)
    assert detail is not None

    embedded_submission = detail.get("submission")
    if isinstance(embedded_submission, dict):
        return detail, embedded_submission

    # A documentação permite que `submission` esteja ausente. Nesse caso, o GET
    # read-only de /submissions/self distingue uma tentativa ainda não enviada.
    fallback = fetch_current_submission(
        session,
        base,
        course_id,
        assignment_id,
    )
    return detail, fallback


def _assignment_ids_from_list(
    session: requests.Session,
    base: str,
    course_id: str,
) -> List[str]:
    url = _url_with_query(
        f"{base}/api/v1/courses/{course_id}/assignments",
        (("include[]", "submission"), ("order_by", "position"), ("per_page", 100)),
    )
    assignments = get_paginated(session, url, trusted_origin=base)
    ids: List[str] = []
    for assignment in assignments:
        assignment_id = assignment.get("id")
        if assignment_id is None:
            raise CanvasAPIError("Atividade retornada sem campo id.")
        ids.append(str(assignment_id))
    return ids


def download_course_assignments(
    session: requests.Session,
    base: str,
    course_id: str,
    course_dir: Path,
    *,
    assignment_ids: Optional[Sequence[str]] = None,
    download_session: Optional[requests.Session] = None,
    metadata_only: bool = False,
    download_failures: Optional[List[str]] = None,
    local_index: Optional[LocalFileIndex] = None,
) -> List[Path]:
    download_session = download_session or create_download_session()
    exercises_dir = course_dir / "exercicios"
    exercises_dir.mkdir(parents=True, exist_ok=True)

    selected_ids = (
        [str(value) for value in assignment_ids]
        if assignment_ids is not None
        else _assignment_ids_from_list(session, base, course_id)
    )
    if not selected_ids:
        print("  [INFO] sem atividades em /assignments.")
        return []

    print(f"  Atividades encontradas: {len(selected_ids)}")
    written: List[Path] = []
    for assignment_id in selected_ids:
        try:
            detail, submission = fetch_assignment_bundle(
                session, base, course_id, assignment_id
            )
        except Exception as exc:
            _track_file_exception(
                f"atividade id={assignment_id}", exc, download_failures
            )
            continue
        assignment_folder = resolve_assignment_folder(exercises_dir, detail)
        assignment_folder.mkdir(parents=True, exist_ok=True)
        assignment_path = assignment_folder / "assignment.json"
        assignment_path.write_text(
            json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append(assignment_path)

        if submission is not None:
            (assignment_folder / "submission.json").write_text(
                json.dumps(submission, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        description = str(detail.get("description") or "")
        links = extract_links_from_html(description)
        if links:
            (assignment_folder / "links.txt").write_text(
                "\n".join(links), encoding="utf-8"
            )

        if metadata_only:
            continue

        file_ids = set(extract_canvas_file_ids_from_html(description))
        attachments = detail.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                try:
                    file_ids.add(int(attachment.get("id")))
                except (TypeError, ValueError):
                    continue

        assignment_name = detail.get("name") or f"assignment_{assignment_id}"
        if not file_ids:
            print(f"    [INFO] sem arquivos Canvas vinculados: {assignment_name}")
            continue

        downloads_dir = assignment_folder / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        for file_id in sorted(file_ids):
            label = f"{assignment_name} -> arquivo id={file_id}"
            try:
                outcome, dest, result = fetch_or_copy_file(
                    session, download_session, base, file_id,
                    downloads_dir, None, local_index,
                )
                _report_file_outcome(
                    label, outcome, dest, result, download_failures,
                    prefix=f"{assignment_name} -> ",
                )
            except Exception as exc:
                _track_file_exception(label, exc, download_failures)
                continue

    return written


def download_course_files(
    session: requests.Session,
    base: str,
    course_id: str,
    course_dir: Path,
    *,
    download_session: Optional[requests.Session] = None,
    metadata_only: bool = False,
    download_failures: Optional[List[str]] = None,
    local_index: Optional[LocalFileIndex] = None,
) -> None:
    download_session = download_session or create_download_session()
    files_dir = course_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    files_url = _url_with_query(
        f"{base}/api/v1/courses/{course_id}/files", (("per_page", 100),)
    )
    files = get_paginated(session, files_url, trusted_origin=base)
    (files_dir / "_files.json").write_text(
        json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not files:
        print("  [INFO] sem arquivos disponíveis em /files.")
        return
    print(f"  Arquivos encontrados em /files: {len(files)}")
    if metadata_only:
        return

    for file_meta in files:
        file_id = file_meta.get("id")
        name = file_meta.get("display_name") or file_meta.get("filename") or file_id
        if file_id is None:
            _track_download_result(
                "arquivo geral sem id",
                DownloadResult(False, "campo id ausente"),
                download_failures,
            )
            continue
        label = f"arquivo geral id={file_id} ({name})"
        try:
            dest = files_dir / canvas_file_local_name(file_id, name)
            if already_downloaded(dest, file_meta.get("size")):
                print(f"    [SKIP] {name}")
                continue
            if local_index is not None and local_index.copy_to(file_id, dest, file_meta.get("size")):
                print(f"    [COPIA] {name} (já existia em outra pasta do curso)")
                continue
            meta = fetch_file_download_url(session, base, int(file_id))
            download_url = meta.get("url")
            if not isinstance(download_url, str) or not download_url:
                _track_download_result(
                    label,
                    DownloadResult(False, "URL de download ausente"),
                    download_failures,
                )
                continue
            result = download_file(
                download_session,
                download_url,
                dest,
                expected_size=meta.get("size"),
            )
            if result.success and local_index is not None:
                local_index.add(file_id, dest)
            _track_download_result(str(name), result, download_failures)
        except Exception as exc:
            _track_file_exception(label, exc, download_failures)
            continue


# =========================
# CLI
# =========================
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Baixa metadados e recursos de cursos/atividades do Canvas."
    )
    parser.add_argument(
        "--course-url",
        action="append",
        default=[],
        help=(
            "URL da UI /courses/{id}/assignments[/] ou "
            "/courses/{id}/assignments/{assignment_id}; pode ser repetida."
        ),
    )
    parser.add_argument(
        "--course-id",
        action="append",
        default=[],
        help="ID numérico de curso; pode ser repetido.",
    )
    parser.add_argument(
        "--only-assignments",
        action="store_true",
        help="Pula módulos e a coleção geral de arquivos do curso.",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Salva JSONs/links, mas não baixa o conteúdo dos arquivos.",
    )
    parser.add_argument(
        "--refresh-courses",
        action="store_true",
        help="Ignora cursos.json e refaz a lista de cursos ativos.",
    )
    parser.add_argument(
        "--out-dir",
        help="Diretório de saída; relativo à pasta deste script quando não absoluto.",
    )
    return parser


def _validate_numeric_id(value: Any, label: str) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"\d+", text):
        raise ValueError(f"{label} deve ser numérico: {value!r}")
    return text


def _merge_selection(
    selections: Dict[str, CourseSelection],
    *,
    course_id: str,
    assignment_id: Optional[str],
    assignments_only: bool,
) -> None:
    existing = selections.get(course_id)
    incoming_ids = {assignment_id} if assignment_id else None
    if existing is None:
        selections[course_id] = CourseSelection(
            course_id=course_id,
            assignment_ids=incoming_ids,
            assignments_only=assignments_only,
        )
        return

    if existing.assignment_ids is None or incoming_ids is None:
        existing.assignment_ids = None
    else:
        existing.assignment_ids.update(incoming_ids)
    existing.assignments_only = existing.assignments_only and assignments_only


def build_explicit_selections(
    course_urls: Sequence[str],
    course_ids: Sequence[str],
    *,
    canvas_base_url: str = DEFAULT_CANVAS_BASE_URL,
) -> Dict[str, CourseSelection]:
    selections: Dict[str, CourseSelection] = {}
    for ui_url in course_urls:
        target = parse_canvas_assignment_url(ui_url, canvas_base_url)
        _merge_selection(
            selections,
            course_id=target.course_id,
            assignment_id=target.assignment_id,
            assignments_only=True,
        )
    for raw_course_id in course_ids:
        course_id = _validate_numeric_id(raw_course_id, "course_id")
        _merge_selection(
            selections,
            course_id=course_id,
            assignment_id=None,
            assignments_only=False,
        )
    return selections


def resolve_out_dir(raw_value: Optional[str], base_dir: Path) -> Path:
    if not raw_value:
        return data_paths.downloads_root()
    candidate = Path(raw_value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def emit_progress(step: str, current: int, total: int, label: str = "") -> None:
    """Linha estruturada lida pelo painel; no terminal aparece como texto comum."""
    payload = {"step": step, "current": current, "total": total, "label": label}
    print("::progress " + json.dumps(payload, ensure_ascii=False), flush=True)


def course_keys_for_selection(
    session: requests.Session,
    base: str,
    courses_json_path: Path,
    course_ids: Sequence[str],
) -> Dict[str, str]:
    """Usa a mesma pasta da sincronização completa para cursos selecionados."""
    cached: Dict[str, str] = {}
    if courses_json_path.exists():
        try:
            data = json.loads(courses_json_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cached = {str(key): str(value) for key, value in data.items()}
        except (OSError, json.JSONDecodeError):
            cached = {}
    reverse = {course_id: key for key, course_id in cached.items()}
    missing = [course_id for course_id in course_ids if course_id not in reverse]
    if missing:
        fetched = []
        for course_id in missing:
            try:
                course = get_json_object(session, f"{base}/api/v1/courses/{course_id}", trusted_origin=base)
            except Exception:
                course = None
            fetched.append(course if isinstance(course, dict) else {"id": course_id, "name": f"CURSO_{course_id}"})
        known_courses = [{"id": course_id, "name": key} for key, course_id in cached.items()]
        cached = build_courses_dict(known_courses + fetched, cached)
        courses_json_path.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
        reverse = {course_id: key for key, course_id in cached.items()}
    return {course_id: reverse.get(course_id, f"CURSO_{course_id}") for course_id in course_ids}


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    base_dir = Path(__file__).resolve().parent
    env_path = base_dir / "res.env"
    courses_json_path = Path(
        os.getenv("PUC_COURSES_CACHE", str(data_paths.courses_cache_path()))
    ).expanduser()
    courses_json_path.parent.mkdir(parents=True, exist_ok=True)
    load_dotenv(dotenv_path=env_path)

    token = os.getenv("TOKEN_CANVAS")
    if not token:
        raise CanvasAPIError("TOKEN_CANVAS não encontrado no res.env.")

    base = normalize_canvas_base_url(os.getenv("CANVAS_BASE_URL"))
    out_dir = resolve_out_dir(args.out_dir or os.getenv("OUT_DIR"), base_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    api_session = requests.Session()
    api_session.headers.update(
        {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    download_session = create_download_session()
    download_failures: List[str] = []

    selections = build_explicit_selections(
        args.course_url,
        args.course_id,
        canvas_base_url=base,
    )

    course_jobs: List[Tuple[str, CourseSelection]] = []
    if selections:
        keys = course_keys_for_selection(
            api_session, base, courses_json_path, list(selections)
        )
        for course_id, selection in selections.items():
            course_jobs.append((keys[course_id], selection))
    else:
        courses = load_or_fetch_courses(
            api_session,
            base,
            courses_json_path,
            refresh=args.refresh_courses,
        )
        for name, course_id in courses.items():
            course_jobs.append(
                (
                    name,
                    CourseSelection(
                        course_id=course_id,
                        assignment_ids=None,
                        assignments_only=False,
                    ),
                )
            )

    total_courses = len(course_jobs)
    for course_index, (course_name, selection) in enumerate(course_jobs, start=1):
        emit_progress("cursos", course_index - 1, total_courses, course_name)
        print(f"=== {course_name} (id={selection.course_id}) ===")
        course_dir = out_dir / safe_filename(course_name)
        course_dir.mkdir(parents=True, exist_ok=True)
        local_index = None if args.metadata_only else LocalFileIndex(course_dir)

        detail_only = selection.assignment_ids is not None
        assignments_only = args.only_assignments or selection.assignments_only or detail_only
        if not assignments_only:
            try:
                download_modules_items(
                    api_session,
                    base,
                    selection.course_id,
                    course_dir,
                    download_session=download_session,
                    metadata_only=args.metadata_only,
                    download_failures=download_failures,
                    local_index=local_index,
                )
            except Exception as exc:
                _track_file_exception(
                    f"curso {selection.course_id} -> módulos",
                    exc,
                    download_failures,
                )

        try:
            download_course_assignments(
                api_session,
                base,
                selection.course_id,
                course_dir,
                assignment_ids=(
                    sorted(selection.assignment_ids)
                    if selection.assignment_ids is not None
                    else None
                ),
                download_session=download_session,
                metadata_only=args.metadata_only,
                download_failures=download_failures,
                local_index=local_index,
            )
        except Exception as exc:
            _track_file_exception(
                f"curso {selection.course_id} -> atividades",
                exc,
                download_failures,
            )

        if not assignments_only:
            try:
                download_course_files(
                    api_session,
                    base,
                    selection.course_id,
                    course_dir,
                    download_session=download_session,
                    metadata_only=args.metadata_only,
                    download_failures=download_failures,
                    local_index=local_index,
                )
            except Exception as exc:
                _track_file_exception(
                    f"curso {selection.course_id} -> arquivos gerais",
                    exc,
                    download_failures,
                )
        print()
    emit_progress("cursos", total_courses, total_courses, "")

    if download_failures:
        print(f"[ERRO] Downloads incompletos: {len(download_failures)}.")
        # O painel mostra "Concluído com avisos"; o exit code 1 continua
        # sinalizando a coleta parcial para quem usa o CLI.
        print("::status partial", flush=True)
        return 1

    # Mantem o CLI compativel com consoles Windows configurados como cp1252.
    print("[OK] Finalizado.")
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (CanvasAPIError, ValueError) as exc:
        raise SystemExit(f"Erro: {exc}") from exc


if __name__ == "__main__":
    main()
