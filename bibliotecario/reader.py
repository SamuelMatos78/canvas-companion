#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from puc_tools.openai_compat import (  # noqa: E402
    OpenAIConfig,
    OpenAIConfigError,
    create_chat_completion,
    parse_json_object,
)
from puc_tools import paths as data_paths  # noqa: E402


SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "ambiente_pratica",
    "resolucao_ia",
    "resumos",
}


@dataclass
class RunStats:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    pdf_render_failed: int = 0
    pdf_render_from_existing: int = 0


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_pdf_files(root: Path) -> Iterable[Path]:
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
        for file_name in files:
            if file_name.lower().endswith(".pdf"):
                yield Path(current_root) / file_name


def filter_pdf_files_by_course(
    pdfs: Iterable[Path],
    root: Path,
    course: Optional[str],
) -> List[Path]:
    if not course:
        return list(pdfs)

    expected = course.strip().casefold()
    selected: List[Path] = []
    for pdf_path in pdfs:
        try:
            parts = pdf_path.relative_to(root).parts
        except ValueError:
            continue
        lower_parts = [part.casefold() for part in parts]
        if "downloads_canvas" in lower_parts:
            index = lower_parts.index("downloads_canvas") + 1
            course_part = parts[index] if index < len(parts) else ""
        else:
            course_part = parts[0] if parts else ""
        if course_part.casefold() == expected:
            selected.append(pdf_path)
    return selected


def drop_duplicate_canvas_files(pdfs: List[Path], root: Path) -> Tuple[List[Path], int]:
    """Mantém um PDF por id do Canvas em cada matéria.

    O coletor guarda o mesmo arquivo em files/, nos módulos e em cada atividade
    que o cita; sem isso, o mesmo material era resumido várias vezes pela IA.
    Prefere a cópia de files/, depois a de modules/.
    """
    def rank(path: Path) -> Tuple[int, str]:
        parts = {part.casefold() for part in path.parts}
        return (0 if "files" in parts else 1 if "modules" in parts else 2, str(path))

    kept: List[Path] = []
    seen: set = set()
    duplicates = 0
    for pdf_path in sorted(pdfs, key=rank):
        match = re.match(r"(\d+)_", pdf_path.name)
        if match:
            key = (str(resolve_course_dir(pdf_path, root)).casefold(), match.group(1))
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
        kept.append(pdf_path)
    return sorted(kept), duplicates


def emit_progress(step: str, current: int, total: int, label: str = "") -> None:
    """Linha estruturada lida pelo painel para mostrar o andamento."""
    payload = {"step": step, "current": current, "total": total, "label": label}
    print("::progress " + json.dumps(payload, ensure_ascii=False), flush=True)


def resolve_course_dir(pdf_path: Path, root: Path) -> Path:
    rel_parts = pdf_path.relative_to(root).parts
    lower_parts = [p.lower() for p in rel_parts]

    if "downloads_canvas" in lower_parts:
        idx = lower_parts.index("downloads_canvas")
        if idx + 1 < len(rel_parts):
            return root.joinpath(*rel_parts[: idx + 2])

    if len(rel_parts) >= 2:
        return root / rel_parts[0]
    return root


def build_summary_stem(pdf_path: Path, course_dir: Path) -> str:
    rel = pdf_path.relative_to(course_dir).with_suffix("")
    flat = "__".join(rel.parts)
    return safe_filename(flat)


def extract_pdf_content(
    pdf_path: Path,
    max_pages: int,
    max_images: int,
    image_max_side: int,
    ocr_lang: str,
) -> Dict[str, Any]:
    text_pages: List[str] = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path), strict=False)
        selected_pages = list(reader.pages[:max_pages])
        for idx, page in enumerate(selected_pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                text_pages.append(f"[pagina {idx}]\n{text}")
        extracted_text = "\n\n".join(text_pages).strip()
        if extracted_text:
            return {
                "total_paginas_processadas": len(selected_pages),
                "texto_extraido": extracted_text,
                "imagens_extraidas": [],
                "metodo_extracao": "texto_pdf",
            }
    except Exception:
        # PDFs com texto corrompido ou apenas imagens ainda podem passar pelo OCR.
        selected_pages = []

    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError as exc:
        raise RuntimeError(
            "Este PDF não tem texto selecionável. Para resumir PDFs digitalizados, "
            "instale Poppler e Tesseract no computador."
        ) from exc

    try:
        pages = convert_from_path(str(pdf_path), dpi=250, first_page=1, last_page=max_pages)
    except Exception as exc:
        if text_pages:
            return {
                "total_paginas_processadas": len(selected_pages),
                "texto_extraido": "\n\n".join(text_pages),
                "imagens_extraidas": [],
                "metodo_extracao": "texto_pdf",
            }
        raise RuntimeError(
            "Não foi possível abrir o PDF para OCR. Verifique se o Poppler está instalado."
        ) from exc

    full_text_parts: List[str] = []
    image_payloads: List[Dict[str, str]] = []

    for idx, page in enumerate(pages, start=1):
        ocr_text = pytesseract.image_to_string(page, lang=ocr_lang).strip()
        full_text_parts.append(f"[pagina {idx}]\n{ocr_text}")

        if idx <= max_images:
            preview = page.copy()
            preview.thumbnail((image_max_side, image_max_side))
            preview_bytes = io.BytesIO()
            preview.save(preview_bytes, format="JPEG", quality=45, optimize=True)
            b64 = base64.b64encode(preview_bytes.getvalue()).decode("ascii")
            image_payloads.append(
                {
                    "pagina": str(idx),
                    "mime_type": "image/jpeg",
                    "base64_jpeg": b64,
                }
            )

    full_text = "\n\n".join(full_text_parts).strip()
    return {
        "total_paginas_processadas": len(pages),
        "texto_extraido": full_text,
        "imagens_extraidas": image_payloads,
        "metodo_extracao": "ocr",
    }


def call_openai_compatible_api(
    config: OpenAIConfig,
    source_pdf: Path,
    extraction: Dict[str, Any],
    max_input_chars: int,
) -> Tuple[Dict[str, Any], str]:
    text_excerpt = extraction["texto_extraido"][:max_input_chars]
    input_payload = {
        "arquivo": source_pdf.name,
        "paginas_processadas": extraction["total_paginas_processadas"],
        "texto_ocr": text_excerpt,
    }

    system_prompt = (
        "Voce e um assistente educacional. Retorne apenas JSON valido, sem markdown."
    )
    user_prompt = (
        "Com base no conteudo extraido, gere resumo e exercicios de fixacao em portugues.\n"
        "Formato JSON obrigatorio:\n"
        "{\n"
        '  "titulo": "string",\n'
        '  "resumo_geral": "string",\n'
        '  "topicos": [{"titulo": "string", "explicacao": "string"}],\n'
        '  "exercicios_fixacao": [{"tipo": "aberta", "pergunta": "string", "gabarito": "string"}]\n'
        "}\n\n"
        f"Dados extraidos:\n{json.dumps(input_payload, ensure_ascii=False)}"
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
        raise RuntimeError("A LLM nao retornou um objeto JSON valido.")
    return structured, content


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def _pick_font(size: int) -> Any:
    from PIL import ImageFont

    windows_arial = Path("C:/Windows/Fonts/arial.ttf")
    if windows_arial.exists():
        return ImageFont.truetype(str(windows_arial), size=size)
    return ImageFont.load_default()


def build_pdf_lines(summary_record: Dict[str, Any]) -> List[str]:
    result = summary_record.get("resultado", {})
    lines: List[str] = []

    lines.append("RESUMO DE ESTUDOS")
    lines.append("")
    lines.append(f"Arquivo fonte: {summary_record.get('source_pdf', '')}")
    lines.append(f"Gerado em: {summary_record.get('generated_at', '')}")
    lines.append("")

    if not isinstance(result, dict):
        lines.append("Resposta nao estruturada:")
        lines.append(str(result))
        return lines

    title = result.get("titulo")
    if title:
        lines.append(f"Titulo: {title}")
        lines.append("")

    resumo_geral = result.get("resumo_geral") or result.get("resumo")
    if resumo_geral:
        lines.append("Resumo geral:")
        lines.extend(textwrap.wrap(str(resumo_geral), width=100))
        lines.append("")

    topicos = _as_list(result.get("topicos") or result.get("topicos_chave"))
    if topicos:
        lines.append("Topicos:")
        for idx, item in enumerate(topicos, start=1):
            if isinstance(item, dict):
                titulo = item.get("titulo", "")
                explicacao = item.get("explicacao", "")
                lines.append(f"{idx}. {titulo}")
                if explicacao:
                    lines.extend(textwrap.wrap(str(explicacao), width=96))
            else:
                lines.append(f"{idx}. {item}")
            lines.append("")

    exercicios = _as_list(result.get("exercicios_fixacao") or result.get("exercicios"))
    if exercicios:
        lines.append("Exercicios de fixacao:")
        for idx, ex in enumerate(exercicios, start=1):
            if isinstance(ex, dict):
                pergunta = ex.get("pergunta", "")
                gabarito = ex.get("gabarito") or ex.get("resposta_esperada", "")
                lines.append(f"{idx}. Pergunta: {pergunta}")
                if gabarito:
                    lines.extend(textwrap.wrap(f"Gabarito: {gabarito}", width=96))
            else:
                lines.append(f"{idx}. {ex}")
            lines.append("")

    quiz = _as_list(result.get("quiz"))
    if quiz:
        lines.append("Quiz:")
        for idx, q in enumerate(quiz, start=1):
            if isinstance(q, dict):
                pergunta = q.get("pergunta", "")
                lines.append(f"{idx}. {pergunta}")
                for option in _as_list(q.get("opcoes")):
                    lines.append(f" - {option}")
                resposta = q.get("resposta_correta", "")
                explicacao = q.get("explicacao", "")
                if resposta:
                    lines.append(f"Resposta: {resposta}")
                if explicacao:
                    lines.extend(textwrap.wrap(f"Explicacao: {explicacao}", width=96))
            else:
                lines.append(f"{idx}. {q}")
            lines.append("")

    if len(lines) <= 6:
        lines.append("A resposta da API nao trouxe seções esperadas.")
        lines.append("Conteudo bruto:")
        lines.append(json.dumps(result, ensure_ascii=False, indent=2))

    return lines


def write_formatted_pdf(summary_record: Dict[str, Any], output_pdf: Path) -> bool:
    try:
        from PIL import Image, ImageDraw

        lines = build_pdf_lines(summary_record)
        normal_font = _pick_font(30)

        page_width, page_height = 1240, 1754
        margin_x, margin_y = 70, 70
        line_height = 42
        max_lines = (page_height - (margin_y * 2)) // line_height

        pages: List[Image.Image] = []
        chunk_start = 0
        while chunk_start < len(lines):
            chunk = lines[chunk_start : chunk_start + max_lines]
            image = Image.new("RGB", (page_width, page_height), "white")
            draw = ImageDraw.Draw(image)

            y = margin_y
            for line in chunk:
                wrapped = textwrap.wrap(str(line), width=95) or [""]
                for wrapped_line in wrapped:
                    draw.text((margin_x, y), wrapped_line, font=normal_font, fill="black")
                    y += line_height

            pages.append(image)
            chunk_start += max_lines

        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        pages[0].save(
            str(output_pdf),
            "PDF",
            resolution=100.0,
            save_all=True,
            append_images=pages[1:],
        )
        return True
    except Exception:
        return False


def maybe_render_pdf_from_existing_json(json_path: Path, pdf_path: Path) -> bool:
    try:
        existing = json.loads(json_path.read_text(encoding="utf-8"))
        return write_formatted_pdf(existing, pdf_path)
    except Exception:
        return False


def process_pdf(
    pdf_path: Path,
    root_dir: Path,
    llm_config: OpenAIConfig,
    max_pages: int,
    max_images: int,
    max_input_chars: int,
    ocr_lang: str,
    force: bool,
) -> Tuple[str, Optional[str]]:
    course_dir = resolve_course_dir(pdf_path, root_dir)
    resumos_dir = course_dir / "resumos"
    resumos_dir.mkdir(parents=True, exist_ok=True)

    stem = build_summary_stem(pdf_path, course_dir)
    json_path = resumos_dir / f"{stem}.json"
    pdf_output = resumos_dir / f"{stem}.pdf"

    if not force:
        if json_path.exists() and not pdf_output.exists():
            rendered = maybe_render_pdf_from_existing_json(json_path, pdf_output)
            if rendered:
                return "pdf_rendered_from_existing", str(pdf_output)
            return "skipped", "JSON ja existe; PDF permaneceu indisponivel."
        if json_path.exists() or pdf_output.exists():
            return "skipped", None

    extraction = extract_pdf_content(
        pdf_path=pdf_path,
        max_pages=max_pages,
        max_images=max_images,
        image_max_side=320,
        ocr_lang=ocr_lang,
    )

    structured, raw_content = call_openai_compatible_api(
        config=llm_config,
        source_pdf=pdf_path,
        extraction=extraction,
        max_input_chars=max_input_chars,
    )

    summary_record = {
        "source_pdf": str(pdf_path),
        "source_pdf_sha256": sha256_file(pdf_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "api_model": llm_config.model,
        "extraction_meta": {
            "pages_processed": extraction["total_paginas_processadas"],
            "input_chars_sent": len(extraction["texto_extraido"][:max_input_chars]),
            "image_previews_extracted": len(extraction["imagens_extraidas"]),
            "image_previews_sent": 0,
        },
        "resultado": structured,
        "raw_model_response": raw_content,
    }

    json_path.write_text(
        json.dumps(summary_record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    rendered = write_formatted_pdf(summary_record, pdf_output)
    if rendered:
        return "processed", str(pdf_output)
    return "processed_json_only", str(json_path)


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    # Valores injetados pelo Canvas Companion têm precedência sobre arquivos
    # locais, para que o modelo escolhido na interface seja usado nos resumos.
    load_env_file(script_dir / ".env", override=False)
    load_env_file(script_dir / "res.env")

    parser = argparse.ArgumentParser(
        description=(
            "Varre PDFs por cursos, cria pasta 'resumos', extrai texto/imagem, "
            "chama uma LLM OpenAI-compatible e salva resumo/exercicios em JSON e PDF."
        )
    )
    parser.add_argument(
        "--root",
        default=os.getenv("PROJECT_ROOT", str(data_paths.downloads_root())),
        help="Pasta raiz para varredura (padrão: biblioteca sincronizada do Canvas).",
    )
    parser.add_argument(
        "--max-pdfs",
        type=int,
        default=int(os.getenv("MAX_PDFS", "0") or "0"),
        help="Limita quantidade de PDFs processados nesta execucao.",
    )
    parser.add_argument(
        "--course",
        help="Resume somente os PDFs da pasta da materia indicada.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocessa mesmo quando ja existe resumo.",
    )
    args = parser.parse_args()

    max_pages = int(os.getenv("MAX_PAGES_PER_PDF", "12"))
    max_images = int(os.getenv("MAX_IMAGES_PER_PDF", "4"))
    max_input_chars = int(os.getenv("MAX_INPUT_CHARS", "24000"))
    ocr_lang = os.getenv("TESSERACT_LANG", "por+eng")

    try:
        llm_config = OpenAIConfig.from_env(default_timeout=120.0)
    except OpenAIConfigError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"[INFO] API da LLM: {llm_config.endpoint_url}")
    print(f"[INFO] Modelo da LLM: {llm_config.model}")

    root_dir = Path(args.root).resolve()
    if not root_dir.exists():
        raise SystemExit(f"Pasta raiz nao encontrada: {root_dir}")

    pdfs = sorted(filter_pdf_files_by_course(iter_pdf_files(root_dir), root_dir, args.course))
    pdfs, duplicates = drop_duplicate_canvas_files(pdfs, root_dir)
    if duplicates:
        print(f"[INFO] Cópias do mesmo arquivo do Canvas ignoradas: {duplicates}")
    if args.max_pdfs > 0:
        pdfs = pdfs[: args.max_pdfs]

    if not pdfs:
        suffix = f" para a materia {args.course!r}" if args.course else ""
        raise SystemExit(f"Nenhum PDF encontrado em: {root_dir}{suffix}")

    stats = RunStats()
    print(f"[INFO] PDFs encontrados: {len(pdfs)}")

    for index, pdf_path in enumerate(pdfs, start=1):
        emit_progress("pdfs", index - 1, len(pdfs), pdf_path.name)
        print(f"\n[{index}/{len(pdfs)}] {pdf_path}")
        try:
            status, detail = process_pdf(
                pdf_path=pdf_path,
                root_dir=root_dir,
                llm_config=llm_config,
                max_pages=max_pages,
                max_images=max_images,
                max_input_chars=max_input_chars,
                ocr_lang=ocr_lang,
                force=args.force,
            )

            if status == "processed":
                stats.processed += 1
                print(f"[OK] Resumo gerado em PDF: {detail}")
            elif status == "processed_json_only":
                stats.processed += 1
                stats.pdf_render_failed += 1
                print(f"[WARN] PDF falhou; JSON mantido: {detail}")
            elif status == "pdf_rendered_from_existing":
                stats.pdf_render_from_existing += 1
                print(f"[OK] PDF gerado a partir de JSON existente: {detail}")
            else:
                stats.skipped += 1
                if detail:
                    print(f"[SKIP] {detail}")
                else:
                    print("[SKIP] Resumo ja existente.")
        except Exception as exc:
            stats.failed += 1
            print(f"[ERRO] Falha ao processar {pdf_path.name}: {exc}")

    emit_progress("pdfs", len(pdfs), len(pdfs))
    print("\n===== RESUMO DA EXECUCAO =====")
    print(f"Processados: {stats.processed}")
    print(f"Pulados: {stats.skipped}")
    print(f"Falhas gerais: {stats.failed}")
    print(f"PDF fallback (JSON mantido): {stats.pdf_render_failed}")
    print(f"PDF criado de JSON existente: {stats.pdf_render_from_existing}")


if __name__ == "__main__":
    main()
