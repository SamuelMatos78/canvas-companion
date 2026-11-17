#!/usr/bin/env python3
"""CLI unica para as consultas e automacoes do Canvas Companion."""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import runpy
import shutil
import sys
from pathlib import Path
from typing import Optional, Sequence

from dashboard import api as dashboard_api


ACTION_COMMANDS = {
    "update": ("atualizar",),
    "assignments": ("atividades",),
    "prepare": ("preparar",),
    "solve": ("responder", "resolver"),
    "export": ("exportar", "converter"),
    "summarize": ("resumir",),
}


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("informe um número inteiro maior que zero")
    return parsed


def _port_number(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("informe uma porta entre 0 e 65535")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Consulta e executa pelo terminal todas as automacoes do Canvas Companion."
    )
    subparsers = parser.add_subparsers(dest="command")

    status = subparsers.add_parser("status", aliases=["estado"], help="Mostra o mesmo resumo de dados do dashboard.")
    status.add_argument("--json", action="store_true", help="Imprime o payload completo como JSON.")

    subparsers.add_parser("actions", aliases=["acoes"], help="Lista as automacoes disponiveis.")
    serve = subparsers.add_parser("serve", aliases=["api"], help="Inicia a API local usada pelo dashboard.")
    serve.add_argument("--port", type=_port_number, default=dashboard_api.PORT, help="Porta local (0 escolhe uma porta livre).")
    serve.add_argument("--no-browser", action="store_true", help="Não abre o navegador ao iniciar.")
    subparsers.add_parser("debug", aliases=["depurar"], help="Mostra um diagnóstico local sem revelar chaves.")
    deliveries = subparsers.add_parser(
        "deadlines",
        aliases=["entregas"],
        help="Retorna em JSON as atividades com entrega em uma janela futura.",
    )
    deliveries.add_argument(
        "--days",
        "--dias",
        type=_positive_int,
        default=14,
        help="Tamanho da janela futura em dias (padrão: 14).",
    )
    deliveries.add_argument(
        "--description-limit",
        "--limite-descricao",
        type=_positive_int,
        default=100,
        help="Máximo de caracteres da descrição (padrão: 100).",
    )

    for action, aliases in ACTION_COMMANDS.items():
        action_parser = subparsers.add_parser(
            action,
            aliases=list(aliases),
            help=dashboard_api.ACTION_LABELS[action] + ".",
        )
        if action in dashboard_api.SCOPED_ACTIONS:
            action_parser.add_argument(
                "--course",
                help="Limita a execução à pasta da matéria indicada.",
            )
        if action in dashboard_api.SCOPED_EXERCISE_ACTIONS:
            action_parser.add_argument(
                "--assignment-id",
                help="Limita a execução à atividade indicada; requer --course.",
            )
        if action in {"solve", "summarize"}:
            action_parser.add_argument(
                "--force",
                action="store_true",
                help="Reprocessa mesmo quando o resultado já existe.",
            )
    return parser


def _canonical_action(command: str) -> Optional[str]:
    if command in ACTION_COMMANDS:
        return command
    for action, aliases in ACTION_COMMANDS.items():
        if command in aliases:
            return action
    return None


def _print_status(as_json: bool) -> None:
    data = dashboard_api.build_dashboard()
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    stats = data["stats"]
    llm = data["llm"]
    canvas = data["canvas"]
    print(f"Canvas Companion - {data['date_label']}")
    print(f"Pendentes: {stats['pending']} | hoje: {stats['today']} | atrasadas: {stats['overdue']}")
    print(f"Concluidas: {stats['completed']} | ambientes prontos: {stats['prepared']}")
    print(f"Materiais: {stats['materials']} | resumos: {stats['summaries']}")
    print(f"Canvas: {'configurado' if canvas['configured'] else 'nao configurado'}")
    print(f"LLM {llm['model']}: {'online' if llm['online'] else 'offline'}")


def _print_debug() -> None:
    config = dashboard_api._safe_config_payload()
    print("Canvas Companion — diagnóstico local")
    print(f"Sistema: {platform.system()} {platform.release()}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Projeto: {dashboard_api.PROJECT_ROOT}")
    print(f"Canvas: {'configurado' if config['canvas']['configured'] else 'não configurado'}")
    print(f"Modelo: {config['llm']['provider_label']} / {config['llm']['model'] or 'não configurado'}")
    print(f"Arquivos sincronizados: {'sim' if dashboard_api.DOWNLOADS_ROOT.is_dir() else 'não'}")
    missing = [] if getattr(sys, "frozen", False) else [
        package
        for package in ("requests", "dotenv", "PIL", "pdf2image", "pytesseract", "pypdf", "reportlab")
        if importlib.util.find_spec(package) is None
    ]
    print(f"Dependências Python: {'ausentes — ' + ', '.join(missing) if missing else 'disponíveis'}")
    print(f"Node.js/npm: {'disponíveis' if shutil.which('node') and shutil.which('npm') else 'não instalados (opcionais para a CLI)'}")
    print("Dica: puc abre a interface; puc status resume os dados; puc serve inicia somente a API.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if getattr(sys, "frozen", False) and arguments[:1] == ["__puc_worker__"]:
        if len(arguments) < 2:
            raise SystemExit("Worker interno sem arquivo de automação.")
        project_root = dashboard_api.PROJECT_ROOT.resolve()
        candidate = (project_root / arguments[1]).resolve()
        try:
            candidate.relative_to(project_root)
        except ValueError as exc:
            raise SystemExit("Worker interno aponta para fora do pacote.") from exc
        if not candidate.is_file():
            raise SystemExit(f"Arquivo da automação não encontrado: {candidate.name}")
        sys.argv = [str(candidate), *arguments[2:]]
        # O executavel ignora PYTHONUTF8; sem isto o painel recebia cp1252 e
        # exibia "Reavalia��o" no log.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except (AttributeError, ValueError):
                pass
        runpy.run_path(str(candidate), run_name="__main__")
        return 0

    args = build_parser().parse_args(arguments)

    if args.command is None:
        dashboard_api.main()
        return 0
    if args.command in {"status", "estado"}:
        _print_status(args.json)
        return 0
    if args.command in {"actions", "acoes"}:
        for action, label in dashboard_api.ACTION_LABELS.items():
            print(f"{action:<12} {label}")
        return 0
    if args.command in {"deadlines", "entregas"}:
        data = dashboard_api.upcoming_deliveries(
            days=args.days,
            description_limit=args.description_limit,
        )
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"serve", "api"}:
        dashboard_api.main(port=args.port, open_browser=not args.no_browser)
        return 0
    if args.command in {"debug", "depurar"}:
        _print_debug()
        return 0

    action = _canonical_action(args.command)
    if action is None:
        raise SystemExit(f"Comando desconhecido: {args.command}")
    print(f"[Canvas Companion] {dashboard_api.ACTION_LABELS[action]}")
    return dashboard_api.run_action_sync(
        action,
        course=getattr(args, "course", None),
        assignment_id=getattr(args, "assignment_id", None),
        force=getattr(args, "force", False),
    )


if __name__ == "__main__":
    raise SystemExit(main())
