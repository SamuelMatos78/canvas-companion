#!/usr/bin/env python3

import argparse
import html
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


# Permite executar tanto ``python -m exercitor.fazer_exerc`` quanto o arquivo
# diretamente, sem exigir a instalacao do projeto como pacote.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from puc_tools.openai_compat import (  # noqa: E402
    OpenAIConfig,
    create_chat_completion,
    parse_json_object,
)
from puc_tools import paths as data_paths  # noqa: E402


@dataclass
class RunStats:
    scanned: int = 0
    prepared: int = 0
    eligible: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    pdf_failed: int = 0
    exported: int = 0


def load_env_file(path: Path, *, override: bool = False) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if override or key not in os.environ:
            os.environ[key] = value


def safe_filename(name: str, max_len: int = 180) -> str:
    clean = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        clean = "arquivo"
    return clean[:max_len]


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def html_to_text(html: str) -> str:
    html = html or ""
    html = re.sub(r"<script.*?>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style.*?>.*?</style>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</(p|li|h1|h2|h3|h4|h5|h6|tr)>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    html = unescape(html)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n\n", html)
    return html.strip()


SUBMITTED_WORKFLOW_STATES = {
    "submitted",
    "graded",
    "pending_review",
}

TEXT_FILE_SUFFIXES = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".ipynb",
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".kt",
    ".kts",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".dart",
    ".scala",
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",
    ".html",
    ".css",
    ".scss",
    ".sql",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".xml",
    ".gradle",
    ".properties",
}
MAX_ZIP_MEMBERS = 2_000
MAX_ZIP_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_ZIP_TEXT_MEMBER_BYTES = 8 * 1024 * 1024


def load_submission(
    exercise_dir: Path,
    assignment: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Carrega a submissao do proprio usuario, priorizando submission.json."""
    submission_path = exercise_dir / "submission.json"
    if submission_path.exists():
        loaded = json.loads(submission_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"submission.json invalido em: {submission_path}")
        nested = loaded.get("submission")
        if isinstance(nested, dict):
            return nested
        return loaded

    embedded = assignment.get("submission")
    return embedded if isinstance(embedded, dict) else None


def is_assignment_active_and_pending(
    assignment: Dict[str, Any],
    now: datetime,
    submission: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    # ``has_submitted_submissions`` indica que ao menos um aluno enviou a
    # atividade; ele nao representa a submissao do usuario atual.
    if submission is None:
        embedded = assignment.get("submission")
        submission = embedded if isinstance(embedded, dict) else None

    submission_state = str((submission or {}).get("workflow_state") or "").lower()
    if submission_state in SUBMITTED_WORKFLOW_STATES:
        return False, f"submission_{submission_state}"

    if assignment.get("published") is False:
        return False, "nao_publicado"

    workflow = str(assignment.get("workflow_state") or "").lower()
    if workflow and workflow not in {"published", "available"}:
        return False, f"workflow_{workflow}"

    if assignment.get("locked_for_user") is True:
        return False, "bloqueado"

    availability_status = assignment.get("availability_status") or {}
    availability = str(availability_status.get("status") or "").lower()
    if availability in {"closed", "locked"}:
        return False, f"status_{availability}"
    if availability == "pending":
        return False, "ainda_nao_liberado"

    unlock_at = parse_dt(assignment.get("unlock_at"))
    if unlock_at and unlock_at > now:
        return False, "ainda_nao_liberado"

    lock_at = parse_dt(assignment.get("lock_at"))
    if lock_at and lock_at <= now and availability != "open":
        return False, "encerrado"

    return True, "ativo"


def emit_progress(step: str, current: int, total: int, label: str = "") -> None:
    """Linha estruturada lida pelo painel para mostrar o andamento."""
    payload = {"step": step, "current": current, "total": total, "label": label}
    print("::progress " + json.dumps(payload, ensure_ascii=False), flush=True)


def iter_exercise_dirs(root: Path) -> Iterable[Tuple[Path, Path]]:
    for course_dir in sorted(root.iterdir()):
        if not course_dir.is_dir():
            continue
        exercises_dir = course_dir / "exercicios"
        if not exercises_dir.exists() or not exercises_dir.is_dir():
            continue
        for exercise_dir in sorted(exercises_dir.iterdir()):
            if exercise_dir.is_dir():
                yield course_dir, exercise_dir


def filter_exercise_targets(
    targets: Iterable[Tuple[Path, Path]],
    course: Optional[str] = None,
    assignment_id: Optional[str] = None,
) -> List[Tuple[Path, Path]]:
    """Seleciona uma materia ou atividade sem depender do nome da pasta."""
    selected: List[Tuple[Path, Path]] = []
    normalized_course = (course or "").strip().casefold()
    normalized_assignment = (assignment_id or "").strip()

    for course_dir, exercise_dir in targets:
        if normalized_course and course_dir.name.casefold() != normalized_course:
            continue
        if normalized_assignment:
            assignment_path = exercise_dir / "assignment.json"
            try:
                assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(assignment, dict) or str(assignment.get("id")) != normalized_assignment:
                continue
        selected.append((course_dir, exercise_dir))
    return selected


def build_practice_statement(assignment: Dict[str, Any]) -> str:
    title = str(assignment.get("name") or "Atividade")
    description = html_to_text(str(assignment.get("description") or ""))
    due_at = assignment.get("due_at") or "nao informado"
    points = assignment.get("points_possible")
    submission_types = assignment.get("submission_types")

    lines = [
        f"# {title}",
        "",
        f"- ID da atividade: {assignment.get('id', 'nao informado')}",
        f"- Prazo informado pelo Canvas: {due_at}",
    ]
    if points is not None:
        lines.append(f"- Pontuacao: {points}")
    if isinstance(submission_types, list) and submission_types:
        lines.append(f"- Formas de entrega: {', '.join(map(str, submission_types))}")

    lines.extend(
        [
            "",
            "## Enunciado",
            "",
            description or "O Canvas nao forneceu um enunciado textual para esta atividade.",
            "",
            "## Area de trabalho",
            "",
            "Use esta pasta para praticar. Os anexos originais estao em `arquivos/`.",
            "Projetos ZIP sao descompactados de forma segura em `projetos/`.",
            "Este arquivo nao contem a resposta recomendada pela IA.",
            "",
        ]
    )
    return "\n".join(lines)


def _safe_zip_member_path(info: zipfile.ZipInfo) -> Path:
    """Valida um membro ZIP contra zip-slip, links e caminhos de dispositivo."""
    normalized = info.filename.replace("\\", "/")
    relative = PurePosixPath(normalized)
    parts = relative.parts
    unix_mode = info.external_attr >> 16

    if (
        not parts
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or any(":" in part for part in parts)
    ):
        raise ValueError(f"Caminho inseguro dentro do ZIP: {info.filename!r}")
    if (unix_mode & 0o170000) == 0o120000:
        raise ValueError(f"Link simbolico recusado dentro do ZIP: {info.filename!r}")

    return Path(*parts)


def _validated_zip_members(archive: zipfile.ZipFile) -> List[Tuple[zipfile.ZipInfo, Path]]:
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ValueError(
            f"ZIP excede o limite de {MAX_ZIP_MEMBERS} entradas: {archive.filename}"
        )

    total_size = sum(max(0, int(info.file_size)) for info in infos)
    if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise ValueError(
            "ZIP excede o limite de tamanho descompactado "
            f"({MAX_ZIP_UNCOMPRESSED_BYTES} bytes): {archive.filename}"
        )

    return [(info, _safe_zip_member_path(info)) for info in infos]


def extract_zip_to_practice(zip_path: Path, destination_root: Path) -> int:
    """Extrai um projeto ZIP sem sobrescrever trabalho existente do usuario."""
    destination_root.mkdir(parents=True, exist_ok=True)
    resolved_root = destination_root.resolve()
    extracted = 0

    with zipfile.ZipFile(zip_path) as archive:
        for info, relative in _validated_zip_members(archive):
            destination = (destination_root / relative).resolve()
            try:
                destination.relative_to(resolved_root)
            except ValueError as exc:
                raise ValueError(
                    f"Membro ZIP escaparia da area de pratica: {info.filename!r}"
                ) from exc

            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if destination.exists():
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            extracted += 1

    return extracted


def prepare_practice_workspace(
    exercise_dir: Path,
    assignment: Dict[str, Any],
) -> Path:
    """Cria uma area de pratica sem alterar downloads nem trabalho existente."""
    practice_dir = exercise_dir / "ambiente_pratica"
    files_dir = practice_dir / "arquivos"
    projects_dir = practice_dir / "projetos"
    practice_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)

    statement_path = practice_dir / "enunciado.md"
    if not statement_path.exists():
        statement_path.write_text(
            build_practice_statement(assignment),
            encoding="utf-8",
        )

    downloads_dir = exercise_dir / "downloads"
    if downloads_dir.exists():
        downloads_root = downloads_dir.resolve()
        for source in sorted(downloads_dir.rglob("*")):
            if not source.is_file():
                continue
            try:
                relative = source.resolve().relative_to(downloads_root)
            except ValueError:
                # Nao segue links que escapem da pasta de downloads.
                continue
            destination = files_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)
            if source.suffix.lower() == ".zip":
                project_dir = projects_dir / safe_filename(source.stem)
                extract_zip_to_practice(source, project_dir)

    return practice_dir


def extract_text_from_pdf(pdf_path: Path, max_pages: int, ocr_lang: str) -> str:
    import pytesseract
    from pdf2image import convert_from_path

    pages = convert_from_path(str(pdf_path), dpi=250, first_page=1, last_page=max_pages)
    parts: List[str] = []
    for index, page in enumerate(pages, start=1):
        text = pytesseract.image_to_string(page, lang=ocr_lang).strip()
        parts.append(f"[pagina {index}]\n{text}")
    return "\n\n".join(parts).strip()


def extract_text_from_image(image_path: Path, ocr_lang: str) -> str:
    import pytesseract
    from PIL import Image

    with Image.open(image_path) as image:
        return pytesseract.image_to_string(image, lang=ocr_lang).strip()


def extract_text_from_text_file(path: Path, max_chars: int) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    return raw[:max_chars].strip()


def extract_text_from_zip(path: Path, max_chars: int) -> str:
    """Le arquivos textuais/codigo de um ZIP sem extrai-los para o prompt."""
    parts: List[str] = []
    with zipfile.ZipFile(path) as archive:
        for info, relative in _validated_zip_members(archive):
            if info.is_dir() or relative.suffix.lower() not in TEXT_FILE_SUFFIXES:
                continue
            if info.file_size > MAX_ZIP_TEXT_MEMBER_BYTES:
                continue

            remaining = max_chars - len("\n\n".join(parts))
            if remaining <= 0:
                break
            with archive.open(info, "r") as member:
                raw = member.read(min(info.file_size, remaining * 4 + 4096))
            text = raw.decode("utf-8", errors="ignore").strip()
            if text:
                parts.append(f"[ARQUIVO_NO_ZIP: {relative.as_posix()}]\n{text}")

    return "\n\n".join(parts)[:max_chars].strip()


def extract_file_content(
    file_path: Path,
    max_pages_per_pdf: int,
    max_chars_per_file: int,
    ocr_lang: str,
) -> Dict[str, Any]:
    suffix = file_path.suffix.lower()

    try:
        if suffix == ".pdf":
            text = extract_text_from_pdf(file_path, max_pages=max_pages_per_pdf, ocr_lang=ocr_lang)
            return {
                "arquivo": file_path.name,
                "tipo": "pdf_ocr",
                "texto": text[:max_chars_per_file],
            }

        if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}:
            text = extract_text_from_image(file_path, ocr_lang=ocr_lang)
            return {
                "arquivo": file_path.name,
                "tipo": "imagem_ocr",
                "texto": text[:max_chars_per_file],
            }

        if suffix in TEXT_FILE_SUFFIXES:
            text = extract_text_from_text_file(file_path, max_chars=max_chars_per_file)
            return {
                "arquivo": file_path.name,
                "tipo": "texto",
                "texto": text,
            }

        if suffix == ".zip":
            text = extract_text_from_zip(file_path, max_chars=max_chars_per_file)
            return {
                "arquivo": file_path.name,
                "tipo": "zip_texto" if text else "zip_sem_texto_suportado",
                "texto": text,
            }

        return {
            "arquivo": file_path.name,
            "tipo": "nao_suportado",
            "texto": "",
        }
    except Exception as exc:
        return {
            "arquivo": file_path.name,
            "tipo": "erro_extracao",
            "texto": "",
            "erro": str(exc),
        }


def call_openai_compatible_api(
    config: OpenAIConfig,
    assignment_payload: Dict[str, Any],
) -> Tuple[Dict[str, Any], str]:
    system_prompt = (
        "Voce e um tutor educacional criterioso. Produza uma resposta final, especifica "
        "para a atividade e baseada apenas no contexto fornecido. Retorne somente um "
        "objeto JSON valido, sem cercas de codigo nem comentarios fora do JSON. O campo "
        "resposta_sugerida deve conter Markdown valido como string JSON."
    )
    user_prompt = (
        "Analise o enunciado, os criterios, o tipo de entrega e os anexos. Gere uma "
        "resposta realmente utilizavel pelo aluno, sem inventar fatos ausentes. Quando "
        "faltar uma informacao indispensavel, registre-a em duvidas_para_confirmar.\n\n"
        "Regras para resposta_sugerida:\n"
        "- escreva a resposta completa em Markdown, pronta para revisao e entrega;\n"
        "- use paragrafos claros, listas e tabelas apenas quando melhorarem a leitura;\n"
        "- use no maximo titulos de nivel 1 e 2 (# e ##), sem uma sequencia de ###;\n"
        "- nao inclua metacomentarios como 'aqui esta a resposta';\n"
        "- adapte o conteudo ao tipo de envio aceito pelo Canvas;\n"
        "- preserve codigo em blocos cercados quando a atividade exigir codigo.\n\n"
        "Formato JSON obrigatorio:\n"
        "{\n"
        '  "titulo_tarefa": "string",\n'
        '  "resumo_do_que_foi_pedido": "string",\n'
        '  "resposta_sugerida": "string",\n'
        '  "passo_a_passo": ["string"],\n'
        '  "checklist_entrega": ["string"],\n'
        '  "duvidas_para_confirmar": ["string"]\n'
        "}\n\n"
        f"Contexto:\n{json.dumps(assignment_payload, ensure_ascii=False)}"
    )

    content = create_chat_completion(
        config=config,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    structured = parse_json_object(content)
    if structured is None:
        raise ValueError("A LLM nao retornou um objeto JSON valido.")

    string_fields = (
        "titulo_tarefa",
        "resumo_do_que_foi_pedido",
        "resposta_sugerida",
    )
    list_fields = (
        "passo_a_passo",
        "checklist_entrega",
        "duvidas_para_confirmar",
    )
    invalid_fields = [
        field
        for field in string_fields
        if not isinstance(structured.get(field), str) or not structured[field].strip()
    ]
    invalid_fields.extend(
        field
        for field in list_fields
        if not isinstance(structured.get(field), list)
        or not all(isinstance(item, str) for item in structured[field])
    )
    if invalid_fields:
        raise ValueError(
            "A resposta JSON da LLM nao segue o schema esperado; campos invalidos: "
            + ", ".join(invalid_fields)
        )
    return structured, content


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def build_markdown(result_record: Dict[str, Any]) -> str:
    result = result_record.get("resultado", {})
    fallback_title = str(result_record.get("exercicio_nome") or "Sugestao de resolucao")

    if not isinstance(result, dict):
        return f"# {fallback_title}\n\n{result}\n"

    title = str(result.get("titulo_tarefa") or fallback_title).strip()
    response = str(result.get("resposta_sugerida") or "").strip()
    # O documento ja fornece o titulo. Evita duplicar um H1 devolvido pela LLM.
    response = re.sub(r"\A\s*#\s+[^\n]+\n+", "", response, count=1)
    lines = [
        f"# {title}",
        "",
        f"**Materia:** {result_record.get('curso', '')}  ",
        f"**Atividade:** {result_record.get('exercicio_nome', '')}",
        "",
    ]

    if response:
        lines.extend([response, ""])
    else:
        lines.extend([str(result.get("resumo_do_que_foi_pedido") or ""), ""])

    checklist = _as_list(result.get("checklist_entrega"))
    questions = _as_list(result.get("duvidas_para_confirmar"))
    steps = _as_list(result.get("passo_a_passo"))
    if steps or checklist or questions:
        lines.extend(["---", "", "## Antes de entregar", ""])
    if steps:
        lines.extend(["**Passos sugeridos**", ""])
        lines.extend(f"{index}. {item}" for index, item in enumerate(steps, start=1))
        lines.append("")
    if checklist:
        lines.extend(["**Checklist**", ""])
        lines.extend(f"- [ ] {item}" for item in checklist)
        lines.append("")
    if questions:
        lines.extend(["**Pontos a confirmar**", ""])
        lines.extend(f"- {item}" for item in questions)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _inline_markdown(value: str) -> str:
    """Converte um subconjunto seguro de Markdown para tags do ReportLab."""
    escaped = html.escape(value, quote=False)
    code_tokens: Dict[str, str] = {}

    def store_code(match: re.Match[str]) -> str:
        token = f"@@CODE{len(code_tokens)}@@"
        code_tokens[token] = f'<font name="Courier">{match.group(1)}</font>'
        return token

    escaped = re.sub(r"`([^`]+)`", store_code, escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", escaped)

    def link(match: re.Match[str]) -> str:
        label, raw_url = match.group(1), html.unescape(match.group(2))
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"}:
            return label
        return f'<link href="{html.escape(raw_url, quote=True)}">{label}</link>'

    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, escaped)
    for token, replacement in code_tokens.items():
        escaped = escaped.replace(token, replacement)
    return escaped


def _register_pdf_fonts() -> Tuple[str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = (
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
    )
    for regular, bold in candidates:
        if regular.exists() and bold.exists():
            try:
                pdfmetrics.registerFont(TTFont("PUCBody", str(regular)))
                pdfmetrics.registerFont(TTFont("PUCBold", str(bold)))
                return "PUCBody", "PUCBold"
            except Exception:
                continue
    return "Helvetica", "Helvetica-Bold"


def _markdown_story(markdown_text: str, styles: Dict[str, Any]) -> List[Any]:
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        ListFlowable,
        ListItem,
        Paragraph,
        Preformatted,
        Spacer,
        Table,
        TableStyle,
    )

    lines = markdown_text.replace("\r\n", "\n").split("\n")
    story: List[Any] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            story.append(Spacer(1, 5))
            index += 1
            continue

        if stripped.startswith("```"):
            language = stripped[3:].strip()
            code: List[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1 if index < len(lines) else 0
            if language:
                story.append(Paragraph(html.escape(language.upper()), styles["code_label"]))
            wrapped_code = [chunk or " " for line in code for chunk in ([line[offset : offset + 98] for offset in range(0, len(line), 98)] or [""])]
            story.append(Preformatted("\n".join(wrapped_code), styles["code"]))
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            level = min(len(heading.group(1)), 3)
            story.append(Paragraph(_inline_markdown(heading.group(2)), styles[f"h{level}"]))
            index += 1
            continue

        if re.fullmatch(r"-{3,}|\*{3,}", stripped):
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#CBD5E1"), spaceBefore=8, spaceAfter=10))
            index += 1
            continue

        if (
            "|" in stripped
            and index + 1 < len(lines)
            and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1])
        ):
            table_rows: List[List[str]] = []
            header_cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            table_rows.append(header_cells)
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                table_rows.append([cell.strip() for cell in lines[index].strip().strip("|").split("|")])
                index += 1
            width = max(len(row) for row in table_rows)
            table_data = []
            for row_index, row in enumerate(table_rows):
                padded = row + [""] * (width - len(row))
                style = styles["table_header"] if row_index == 0 else styles["table_cell"]
                table_data.append([Paragraph(_inline_markdown(cell), style) for cell in padded])
            table = Table(table_data, colWidths=[172 * mm / width] * width, repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF8")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#183153")),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.extend([table, Spacer(1, 8)])
            continue

        unordered = re.match(r"^[-*+]\s+(?:\[([ xX])\]\s+)?(.+)$", stripped)
        ordered = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if unordered or ordered:
            items: List[Any] = []
            is_ordered = bool(ordered)
            while index < len(lines):
                current = lines[index].strip()
                match = re.match(r"^\d+[.)]\s+(.+)$", current) if is_ordered else re.match(r"^[-*+]\s+(?:\[([ xX])\]\s+)?(.+)$", current)
                if not match:
                    break
                if is_ordered:
                    item_text = match.group(1)
                else:
                    checked = match.group(1)
                    item_text = match.group(2)
                    if checked is not None:
                        item_text = ("[x] " if checked.lower() == "x" else "[ ] ") + item_text
                items.append(ListItem(Paragraph(_inline_markdown(item_text), styles["body"]), leftIndent=12))
                index += 1
            list_options: Dict[str, Any] = {
                "bulletType": "1" if is_ordered else "bullet",
                "leftIndent": 22,
                "bulletFontName": styles["body"].fontName,
                "bulletFontSize": 9.5,
                "spaceAfter": 6,
            }
            if is_ordered:
                list_options["start"] = "1"
            else:
                list_options["bulletChar"] = "•"
            story.append(ListFlowable(items, **list_options))
            continue

        if stripped.startswith(">"):
            quote_lines: List[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote_lines.append(lines[index].strip()[1:].strip())
                index += 1
            story.append(Paragraph(_inline_markdown(" ".join(quote_lines)), styles["quote"]))
            continue

        paragraph_lines = [stripped]
        index += 1
        while index < len(lines) and lines[index].strip():
            candidate = lines[index].strip()
            if re.match(r"^(#{1,6})\s+|^```|^[-*+]\s+|^\d+[.)]\s+|^>|^-{3,}$", candidate):
                break
            if "|" in candidate and index + 1 < len(lines) and re.search(r"-{3,}", lines[index + 1]):
                break
            paragraph_lines.append(candidate)
            index += 1
        story.append(Paragraph(_inline_markdown(" ".join(paragraph_lines)), styles["body"]))

    return story


def write_formatted_pdf(result_record: Dict[str, Any], output_pdf: Path) -> bool:
    """Renderiza o Markdown da resposta em um PDF vetorial e paginado."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        regular_font, bold_font = _register_pdf_fonts()
        palette = {
            "ink": colors.HexColor("#172033"),
            "muted": colors.HexColor("#5D6B82"),
            "navy": colors.HexColor("#183153"),
            "blue": colors.HexColor("#2F6FED"),
            "pale": colors.HexColor("#F3F6FB"),
            "line": colors.HexColor("#D7DFEA"),
        }
        styles: Dict[str, Any] = {
            "title": ParagraphStyle("Title", fontName=bold_font, fontSize=23, leading=28, textColor=palette["navy"], spaceAfter=8),
            "subtitle": ParagraphStyle("Subtitle", fontName=regular_font, fontSize=9, leading=13, textColor=palette["muted"]),
            "h1": ParagraphStyle("H1", fontName=bold_font, fontSize=19, leading=23, textColor=palette["navy"], spaceBefore=13, spaceAfter=7),
            "h2": ParagraphStyle("H2", fontName=bold_font, fontSize=14, leading=18, textColor=palette["blue"], spaceBefore=12, spaceAfter=5),
            "h3": ParagraphStyle("H3", fontName=bold_font, fontSize=11, leading=15, textColor=palette["navy"], spaceBefore=9, spaceAfter=4),
            "body": ParagraphStyle("Body", fontName=regular_font, fontSize=10.2, leading=15.2, textColor=palette["ink"], alignment=TA_LEFT, spaceAfter=5, splitLongWords=True),
            "quote": ParagraphStyle("Quote", fontName=regular_font, fontSize=9.8, leading=14.5, leftIndent=12, rightIndent=8, borderColor=palette["blue"], borderWidth=0, borderPadding=(7, 8, 7, 12), backColor=palette["pale"], textColor=palette["muted"], spaceBefore=5, spaceAfter=7),
            "code": ParagraphStyle("Code", fontName="Courier", fontSize=7.7, leading=10.2, leftIndent=7, rightIndent=7, borderColor=palette["line"], borderWidth=0.5, borderPadding=7, backColor=colors.HexColor("#F7F9FC"), textColor=palette["ink"], spaceBefore=3, spaceAfter=8),
            "code_label": ParagraphStyle("CodeLabel", fontName=bold_font, fontSize=7, leading=9, textColor=palette["muted"], spaceBefore=5, spaceAfter=2),
            "table_header": ParagraphStyle("TableHeader", fontName=bold_font, fontSize=8.3, leading=11, textColor=palette["navy"]),
            "table_cell": ParagraphStyle("TableCell", fontName=regular_font, fontSize=8.3, leading=11, textColor=palette["ink"]),
            "footer": ParagraphStyle("Footer", fontName=regular_font, fontSize=7.5, leading=9, textColor=palette["muted"], alignment=TA_CENTER),
        }

        result = result_record.get("resultado") if isinstance(result_record.get("resultado"), dict) else {}
        title = str(result.get("titulo_tarefa") or result_record.get("exercicio_nome") or "Resolucao sugerida")
        course = str(result_record.get("curso") or "Materia nao informada").replace("_", " ")
        exercise = str(result_record.get("exercicio_nome") or title)
        generated = str(result_record.get("generated_at") or "")[:10]
        delivery = result_record.get("entrega_canvas") if isinstance(result_record.get("entrega_canvas"), dict) else {}
        submission_type = str(delivery.get("tipo_selecionado") or "nao informado")

        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        document = SimpleDocTemplate(
            str(output_pdf),
            pagesize=A4,
            rightMargin=19 * mm,
            leftMargin=19 * mm,
            topMargin=19 * mm,
            bottomMargin=18 * mm,
            title=title,
            author="Canvas Companion",
            subject=exercise,
        )

        story: List[Any] = [
            Paragraph("CANVAS COMPANION  /  RESOLUCAO PARA REVISAO", styles["subtitle"]),
            Spacer(1, 4),
            Paragraph(html.escape(title), styles["title"]),
        ]
        metadata = [
            [Paragraph("MATERIA", styles["table_header"]), Paragraph("ATIVIDADE", styles["table_header"])],
            [Paragraph(html.escape(course), styles["table_cell"]), Paragraph(html.escape(exercise), styles["table_cell"])],
            [Paragraph("FORMATO DE ENTREGA", styles["table_header"]), Paragraph("GERADO EM", styles["table_header"])],
            [Paragraph(html.escape(submission_type), styles["table_cell"]), Paragraph(html.escape(generated or "nao informado"), styles["table_cell"])],
        ]
        meta_table = Table(metadata, colWidths=[76 * mm, 76 * mm], hAlign="LEFT")
        meta_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), palette["pale"]),
            ("BOX", (0, 0), (-1, -1), 0.5, palette["line"]),
            ("INNERGRID", (0, 0), (-1, -1), 0.35, palette["line"]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.extend([meta_table, Spacer(1, 13)])

        response_markdown = str(result.get("resposta_sugerida") or result.get("resumo_do_que_foi_pedido") or "Sem resposta gerada.")
        response_markdown = re.sub(r"\A\s*#\s+[^\n]+\n+", "", response_markdown, count=1)
        story.extend(_markdown_story(response_markdown, styles))

        steps = _as_list(result.get("passo_a_passo"))
        checklist = _as_list(result.get("checklist_entrega"))
        questions = _as_list(result.get("duvidas_para_confirmar"))
        review_story: List[Any] = []
        if steps or checklist or questions:
            review_story.extend([Spacer(1, 8), Paragraph("Antes de entregar", styles["h2"])])
        if steps:
            review_story.extend(_markdown_story("**Passos sugeridos**\n\n" + "\n".join(f"{i}. {item}" for i, item in enumerate(steps, 1)), styles))
        if checklist:
            review_story.extend(_markdown_story("**Checklist**\n\n" + "\n".join(f"- [ ] {item}" for item in checklist), styles))
        if questions:
            review_story.extend(_markdown_story("**Pontos a confirmar**\n\n" + "\n".join(f"- {item}" for item in questions), styles))
        if review_story:
            story.append(KeepTogether(review_story))

        def decorate_page(canvas: Any, doc: Any) -> None:
            canvas.saveState()
            width, height = A4
            canvas.setFillColor(palette["blue"])
            canvas.rect(0, height - 5 * mm, width, 5 * mm, fill=1, stroke=0)
            canvas.setStrokeColor(palette["line"])
            canvas.line(19 * mm, 13 * mm, width - 19 * mm, 13 * mm)
            canvas.setFillColor(palette["muted"])
            canvas.setFont(regular_font, 7.5)
            canvas.drawString(19 * mm, 8.5 * mm, "Conteudo gerado por IA - revise antes de enviar")
            canvas.drawRightString(width - 19 * mm, 8.5 * mm, f"Pagina {doc.page}")
            canvas.restoreState()

        document.build(story, onFirstPage=decorate_page, onLaterPages=decorate_page)
        return output_pdf.exists() and output_pdf.stat().st_size > 0
    except Exception as exc:
        print(f"[WARN] Nao foi possivel renderizar o PDF: {exc}")
        return False


def _inline_markdown_to_html(value: str) -> str:
    escaped = html.escape(value, quote=False)
    code_tokens: Dict[str, str] = {}

    def store_code(match: re.Match[str]) -> str:
        token = f"@@HTMLCODE{len(code_tokens)}@@"
        code_tokens[token] = f"<code>{match.group(1)}</code>"
        return token

    escaped = re.sub(r"`([^`]+)`", store_code, escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)

    def link(match: re.Match[str]) -> str:
        label, raw_url = match.group(1), html.unescape(match.group(2))
        if urlparse(raw_url).scheme not in {"http", "https"}:
            return label
        return f'<a href="{html.escape(raw_url, quote=True)}">{label}</a>'

    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, escaped)
    for token, replacement in code_tokens.items():
        escaped = escaped.replace(token, replacement)
    return escaped


def markdown_to_canvas_html(markdown_text: str) -> str:
    """Gera o fragmento HTML esperado por uma entrega ``online_text_entry``."""
    lines = markdown_text.replace("\r\n", "\n").split("\n")
    blocks: List[str] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("```"):
            language = stripped[3:].strip()
            code: List[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1 if index < len(lines) else 0
            language_attr = f' class="language-{html.escape(language, quote=True)}"' if language else ""
            blocks.append(f"<pre><code{language_attr}>{html.escape(chr(10).join(code))}</code></pre>")
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            level = 2 if len(heading.group(1)) <= 2 else min(len(heading.group(1)), 4)
            blocks.append(f"<h{level}>{_inline_markdown_to_html(heading.group(2))}</h{level}>")
            index += 1
            continue
        if re.fullmatch(r"-{3,}|\*{3,}", stripped):
            blocks.append("<hr>")
            index += 1
            continue
        if (
            "|" in stripped
            and index + 1 < len(lines)
            and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1])
        ):
            header_cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            index += 2
            body_rows: List[List[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                body_rows.append([cell.strip() for cell in lines[index].strip().strip("|").split("|")])
                index += 1
            header = "".join(f"<th>{_inline_markdown_to_html(cell)}</th>" for cell in header_cells)
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline_markdown_to_html(cell)}</td>" for cell in row) + "</tr>"
                for row in body_rows
            )
            blocks.append(f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>")
            continue
        unordered = re.match(r"^[-*+]\s+(?:\[([ xX])\]\s+)?(.+)$", stripped)
        ordered = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if unordered or ordered:
            tag = "ol" if ordered else "ul"
            items: List[str] = []
            while index < len(lines):
                current = lines[index].strip()
                match = re.match(r"^\d+[.)]\s+(.+)$", current) if ordered else re.match(r"^[-*+]\s+(?:\[([ xX])\]\s+)?(.+)$", current)
                if not match:
                    break
                if ordered:
                    item_text = match.group(1)
                else:
                    checked = match.group(1)
                    item_text = match.group(2)
                    if checked is not None:
                        item_text = ("☑ " if checked.lower() == "x" else "☐ ") + item_text
                items.append(f"<li>{_inline_markdown_to_html(item_text)}</li>")
                index += 1
            blocks.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        if stripped.startswith(">"):
            quote: List[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip()[1:].strip())
                index += 1
            blocks.append(f"<blockquote>{_inline_markdown_to_html(' '.join(quote))}</blockquote>")
            continue

        paragraph = [stripped]
        index += 1
        while index < len(lines) and lines[index].strip():
            candidate = lines[index].strip()
            if re.match(r"^(#{1,6})\s+|^```|^[-*+]\s+|^\d+[.)]\s+|^>|^-{3,}$", candidate):
                break
            paragraph.append(candidate)
            index += 1
        blocks.append(f"<p>{_inline_markdown_to_html(' '.join(paragraph))}</p>")
    return "\n".join(blocks)


def normalize_submission_types(assignment: Dict[str, Any]) -> List[str]:
    raw_types = assignment.get("submission_types")
    if not isinstance(raw_types, list):
        return []
    aliases = {"media_record": "media_recording"}
    normalized: List[str] = []
    for raw_type in raw_types:
        value = aliases.get(str(raw_type).strip().lower(), str(raw_type).strip().lower())
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def build_canvas_delivery(
    assignment: Dict[str, Any],
    result_record: Dict[str, Any],
) -> Dict[str, Any]:
    """Converte a resposta Markdown em metadados do tipo de envio do Canvas.

    O projeto continua sem enviar nada. Para upload, o ``file_id`` permanece
    pendente porque ele so existe depois do fluxo de upload do Canvas.
    """
    result = result_record.get("resultado") if isinstance(result_record.get("resultado"), dict) else {}
    response_markdown = str(result.get("resposta_sugerida") or "").strip()
    accepted = normalize_submission_types(assignment)
    directly_adaptable = {"online_text_entry", "online_upload", "online_url"}
    selected = next((item for item in accepted if item in directly_adaptable), None)
    if selected is None:
        selected = accepted[0] if accepted else "none"

    delivery: Dict[str, Any] = {
        "tipos_aceitos": accepted,
        "tipo_selecionado": selected,
        "conteudo_origem": "resultado.resposta_sugerida",
        "markdown_preservado": True,
        "envio_automatico": False,
        "requer_revisao_humana": True,
    }

    if selected == "online_text_entry":
        body = markdown_to_canvas_html(response_markdown)
        delivery.update({
            "arquivo_principal": "resposta_para_canvas.html",
            "mime_type": "text/html",
            "parametros_api": {
                "submission[submission_type]": "online_text_entry",
                "submission[body]": body,
            },
            "pendencias": [],
        })
    elif selected == "online_upload":
        delivery.update({
            "arquivo_principal": "resultado_ia.pdf",
            "mime_type": "application/pdf",
            "parametros_api": {
                "submission[submission_type]": "online_upload",
                "submission[file_ids][]": [],
            },
            "pendencias": ["Fazer upload do PDF no Canvas e preencher submission[file_ids][]."],
        })
    elif selected == "online_url":
        delivery.update({
            "arquivo_principal": "resultado_ia.md",
            "mime_type": "text/markdown",
            "parametros_api": {
                "submission[submission_type]": "online_url",
                "submission[url]": None,
            },
            "pendencias": ["Publicar o trabalho em uma URL http(s) e preencher submission[url]."],
        })
    elif selected == "media_recording":
        delivery.update({
            "arquivo_principal": "resultado_ia.md",
            "mime_type": "text/markdown",
            "parametros_api": {
                "submission[submission_type]": "media_recording",
                "submission[media_comment_id]": None,
                "submission[media_comment_type]": "video",
            },
            "pendencias": ["Gravar a apresentacao e informar o media_comment_id do Canvas."],
        })
    elif selected == "student_annotation":
        delivery.update({
            "arquivo_principal": "resultado_ia.pdf",
            "mime_type": "application/pdf",
            "parametros_api": {
                "submission[submission_type]": "student_annotation",
                "submission[annotatable_attachment_id]": assignment.get("annotatable_attachment_id"),
            },
            "pendencias": ["Transferir a resposta para o anexo anotavel definido pela atividade."],
        })
    else:
        delivery.update({
            "arquivo_principal": "resultado_ia.pdf",
            "mime_type": "application/pdf",
            "parametros_api": None,
            "pendencias": ["Este tipo de atividade nao possui conversao direta para envio pela API."],
        })
    return delivery


def write_resolution_outputs(
    result_record: Dict[str, Any],
    assignment: Dict[str, Any],
    resolution_dir: Path,
) -> bool:
    """Materializa JSON, Markdown, HTML (quando aplicavel) e PDF."""
    result_record["entrega_canvas"] = build_canvas_delivery(assignment, result_record)
    resolution_dir.mkdir(parents=True, exist_ok=True)

    markdown_path = resolution_dir / "resultado_ia.md"
    pdf_path = resolution_dir / "resultado_ia.pdf"
    markdown_path.write_text(build_markdown(result_record), encoding="utf-8")

    delivery = result_record["entrega_canvas"]
    if delivery.get("tipo_selecionado") == "online_text_entry":
        html_body = delivery.get("parametros_api", {}).get("submission[body]", "")
        (resolution_dir / "resposta_para_canvas.html").write_text(str(html_body), encoding="utf-8")

    pdf_ok = write_formatted_pdf(result_record, pdf_path)
    delivery["pdf_gerado"] = pdf_ok
    (resolution_dir / "resultado_ia.json").write_text(
        json.dumps(result_record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return pdf_ok


def build_limited_context(
    description_text: str,
    extracted_files: List[Dict[str, Any]],
    max_chars: int,
) -> Tuple[str, List[Dict[str, Any]]]:
    parts: List[str] = []
    if description_text:
        parts.append(f"[DESCRICAO_DA_TAREFA]\n{description_text}")

    file_summaries: List[Dict[str, Any]] = []
    for extracted in extracted_files:
        text = str(extracted.get("texto") or "").strip()
        summary = {key: value for key, value in extracted.items() if key != "texto"}
        summary["caracteres_extraidos"] = len(text)
        file_summaries.append(summary)
        if text:
            parts.append(f"[ARQUIVO: {extracted.get('arquivo', '')}]\n{text}")

    combined = "\n\n".join(parts).strip()
    if combined:
        combined = combined[: max(0, max_chars)]
    else:
        combined = "Sem texto extraido; usar somente os metadados da tarefa."
        combined = combined[: max(0, max_chars)]
    return combined, file_summaries


def process_exercise(
    course_dir: Path,
    exercise_dir: Path,
    llm_config: Optional[OpenAIConfig],
    now: datetime,
    max_pages_per_pdf: int,
    max_chars_per_file: int,
    max_total_input_chars: int,
    max_download_files: int,
    ocr_lang: str,
    force: bool,
    prepare_only: bool = False,
    export_existing: bool = False,
) -> Tuple[str, Optional[str]]:
    assignment_path = exercise_dir / "assignment.json"
    if not assignment_path.exists():
        return "skipped", "assignment.json ausente"

    assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
    if not isinstance(assignment, dict):
        raise ValueError(f"assignment.json invalido em: {assignment_path}")

    practice_dir = prepare_practice_workspace(exercise_dir, assignment)
    if prepare_only:
        return "prepared", str(practice_dir)

    resolution_dir = exercise_dir / "resolucao_ia"
    output_json = resolution_dir / "resultado_ia.json"
    output_markdown = resolution_dir / "resultado_ia.md"
    output_pdf = resolution_dir / "resultado_ia.pdf"

    if export_existing:
        if not output_json.exists():
            return "skipped", f"resultado_ia.json ausente em: {resolution_dir}"
        existing = json.loads(output_json.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            raise ValueError(f"resultado_ia.json invalido em: {output_json}")
        pdf_ok = write_resolution_outputs(existing, assignment, resolution_dir)
        return ("exported" if pdf_ok else "exported_without_pdf"), str(resolution_dir)

    submission = load_submission(exercise_dir, assignment)
    is_eligible, reason = is_assignment_active_and_pending(
        assignment,
        now=now,
        submission=submission,
    )
    if not is_eligible:
        return "skipped", f"{reason}; ambiente de pratica: {practice_dir}"

    if output_json.exists() and not force:
        existing = json.loads(output_json.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            raise ValueError(f"resultado_ia.json invalido em: {output_json}")
        needs_export = (
            not output_markdown.exists()
            or not output_pdf.exists()
            or not isinstance(existing.get("entrega_canvas"), dict)
        )
        if needs_export:
            pdf_ok = write_resolution_outputs(existing, assignment, resolution_dir)
            return ("exported" if pdf_ok else "exported_without_pdf"), str(resolution_dir)
        return "skipped", f"resultado ja existe em: {resolution_dir}"

    if llm_config is None:
        raise RuntimeError("Configuracao de LLM ausente; use --prepare-only para nao chamar IA.")

    description_text = html_to_text(str(assignment.get("description") or ""))

    download_dir = exercise_dir / "downloads"
    download_files = (
        sorted([p for p in download_dir.rglob("*") if p.is_file()])
        if download_dir.exists()
        else []
    )
    download_files = download_files[:max_download_files]

    extracted_files: List[Dict[str, Any]] = []

    for file_path in download_files:
        extracted = extract_file_content(
            file_path=file_path,
            max_pages_per_pdf=max_pages_per_pdf,
            max_chars_per_file=max_chars_per_file,
            ocr_lang=ocr_lang,
        )
        extracted_files.append(extracted)

    combined_input, extracted_file_summaries = build_limited_context(
        description_text=description_text,
        extracted_files=extracted_files,
        max_chars=max_total_input_chars,
    )

    payload_to_ai = {
        "curso": course_dir.name,
        "exercicio_pasta": exercise_dir.name,
        "tarefa_nome": assignment.get("name", ""),
        "tarefa_id": assignment.get("id"),
        "pontos_possiveis": assignment.get("points_possible"),
        "tipos_de_envio_aceitos": normalize_submission_types(assignment),
        "criterios_de_avaliacao": assignment.get("rubric") or assignment.get("rubric_settings") or [],
        "tentativas_permitidas": assignment.get("allowed_attempts"),
        "contexto": combined_input,
        "arquivos": extracted_file_summaries,
    }

    structured, raw_content = call_openai_compatible_api(
        config=llm_config,
        assignment_payload=payload_to_ai,
    )

    result_record = {
        "curso": course_dir.name,
        "exercicio_nome": assignment.get("name", exercise_dir.name),
        "exercicio_pasta": exercise_dir.name,
        "assignment_id": assignment.get("id"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "llm_model": llm_config.model,
        "assignment_meta": {
            "due_at": assignment.get("due_at"),
            "unlock_at": assignment.get("unlock_at"),
            "lock_at": assignment.get("lock_at"),
            "submission_types": normalize_submission_types(assignment),
            "submission_workflow_state": (submission or {}).get("workflow_state"),
            "availability_status": (assignment.get("availability_status") or {}).get("status"),
        },
        "input_meta": {
            "download_files_considered": len(download_files),
            "text_chars_sent": len(combined_input),
        },
        "resultado": structured,
        "raw_model_response": raw_content,
    }

    pdf_ok = write_resolution_outputs(result_record, assignment, resolution_dir)
    if pdf_ok:
        return "processed", str(resolution_dir)
    return "processed_without_pdf", str(resolution_dir)


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent

    # Variáveis injetadas pelo serviço local (configuração da interface) têm precedência.
    load_env_file(script_dir / ".env", override=False)
    load_env_file(script_dir / "res.env")

    parser = argparse.ArgumentParser(
        description=(
            "Prepara ambientes limpos para as atividades e, quando solicitado, "
            "gera uma resolucao com qualquer API compativel com OpenAI."
        )
    )
    parser.add_argument(
        "--root",
        default=os.getenv("EXERCISE_ROOT", str(data_paths.downloads_root())),
        help="Pasta raiz que contem os cursos com subpastas de exercicios.",
    )
    parser.add_argument(
        "--max-exercises",
        type=int,
        default=int(os.getenv("MAX_EXERCISES", "0") or "0"),
        help="Limita a quantidade de exercicios avaliados nesta execucao.",
    )
    parser.add_argument(
        "--course",
        help="Processa somente a pasta da materia indicada.",
    )
    parser.add_argument(
        "--assignment-id",
        help="Processa somente a atividade com este ID do Canvas.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocessa mesmo quando resultado ja existe.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare-only",
        action="store_true",
        help=(
            "Cria ambiente_pratica para todas as atividades, inclusive encerradas "
            "ou ja enviadas, sem configurar nem chamar uma LLM."
        ),
    )
    mode.add_argument(
        "--export-existing",
        action="store_true",
        help=(
            "Sem chamar a LLM, reconverte resultado_ia.json para o tipo de envio "
            "aceito e recria Markdown, HTML quando aplicavel e PDF."
        ),
    )
    args = parser.parse_args()

    ocr_lang = os.getenv("TESSERACT_LANG", "por+eng")

    max_pages_per_pdf = int(os.getenv("MAX_PAGES_PER_PDF", "12"))
    max_chars_per_file = int(os.getenv("MAX_CHARS_PER_FILE", "12000"))
    max_total_input_chars = int(os.getenv("MAX_INPUT_CHARS", "30000"))
    max_download_files = int(os.getenv("MAX_DOWNLOAD_FILES", "8"))

    root_dir = Path(args.root).resolve()
    if not root_dir.exists():
        raise SystemExit(f"Pasta raiz nao encontrada: {root_dir}")

    exercise_targets = filter_exercise_targets(
        iter_exercise_dirs(root_dir),
        course=args.course,
        assignment_id=args.assignment_id,
    )
    if args.max_exercises > 0:
        exercise_targets = exercise_targets[: args.max_exercises]

    if not exercise_targets:
        scope = ""
        if args.course:
            scope += f" materia={args.course!r}"
        if args.assignment_id:
            scope += f" assignment_id={args.assignment_id!r}"
        raise SystemExit(f"Nenhum exercicio encontrado para o filtro:{scope or f' root={root_dir}'}")

    llm_config: Optional[OpenAIConfig] = None
    if not args.prepare_only and not args.export_existing:
        try:
            llm_config = OpenAIConfig.from_env(default_timeout=180.0)
        except ValueError as exc:
            raise SystemExit(f"Configuracao de LLM invalida: {exc}") from exc

    stats = RunStats(scanned=len(exercise_targets))
    now = datetime.now(timezone.utc)
    print(f"[INFO] Exercicios encontrados: {stats.scanned}")

    for index, (course_dir, exercise_dir) in enumerate(exercise_targets, start=1):
        emit_progress("atividades", index - 1, len(exercise_targets), f"{course_dir.name} · {exercise_dir.name}")
        print(f"\n[{index}/{len(exercise_targets)}] {course_dir.name} -> {exercise_dir.name}")
        try:
            status, detail = process_exercise(
                course_dir=course_dir,
                exercise_dir=exercise_dir,
                llm_config=llm_config,
                now=now,
                max_pages_per_pdf=max_pages_per_pdf,
                max_chars_per_file=max_chars_per_file,
                max_total_input_chars=max_total_input_chars,
                max_download_files=max_download_files,
                ocr_lang=ocr_lang,
                force=args.force,
                prepare_only=args.prepare_only,
                export_existing=args.export_existing,
            )

            if (exercise_dir / "ambiente_pratica" / "enunciado.md").exists():
                stats.prepared += 1

            if status == "prepared":
                print(f"[OK] Ambiente de pratica preparado: {detail}")
            elif status == "processed":
                stats.eligible += 1
                stats.processed += 1
                print(f"[OK] JSON + Markdown + PDF gerados: {detail}")
            elif status == "processed_without_pdf":
                stats.eligible += 1
                stats.processed += 1
                stats.pdf_failed += 1
                print(f"[WARN] PDF falhou; JSON + Markdown mantidos: {detail}")
            elif status == "exported":
                stats.exported += 1
                print(f"[OK] Formatos de entrega recriados: {detail}")
            elif status == "exported_without_pdf":
                stats.exported += 1
                stats.pdf_failed += 1
                print(f"[WARN] Entrega convertida, mas o PDF falhou: {detail}")
            else:
                stats.skipped += 1
                if detail:
                    print(f"[SKIP] {detail}")
                else:
                    print("[SKIP]")
        except Exception as exc:
            stats.failed += 1
            print(f"[ERRO] {exc}")

    emit_progress("atividades", len(exercise_targets), len(exercise_targets))
    print("\n===== RESUMO DA EXECUCAO =====")
    print(f"Escaneados: {stats.scanned}")
    print(f"Ambientes de pratica preparados: {stats.prepared}")
    print(f"Elegiveis (ativos e nao enviados): {stats.eligible}")
    print(f"Processados: {stats.processed}")
    print(f"Reexportados: {stats.exported}")
    print(f"Pulados: {stats.skipped}")
    print(f"Falhas gerais: {stats.failed}")
    print(f"PDF fallback (JSON mantido): {stats.pdf_failed}")

    if stats.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
