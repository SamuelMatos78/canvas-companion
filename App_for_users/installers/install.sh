#!/usr/bin/env bash
set -Eeuo pipefail

# Configure once before publishing on your portfolio.
DEFAULT_SOURCE_URL=""
source_url="${PUC_SOURCE_URL:-$DEFAULT_SOURCE_URL}"
if [[ -z "$source_url" ]]; then
  read -r -p "Cole o link público do ZIP de código-fonte no Google Drive: " source_url
fi
if [[ ! "$source_url" =~ ^https?:// ]]; then
  echo "Informe um link HTTP ou HTTPS do Google Drive." >&2
  exit 2
fi

python_bin="$(command -v python3 || true)"
if [[ -z "$python_bin" ]]; then
  echo "Python 3.10 ou superior é necessário. Instale Python e execute novamente." >&2
  exit 2
fi
python_version="$("$python_bin" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 ou superior é necessário (encontrado: $python_version)." >&2
  exit 2
fi
command -v curl >/dev/null || { echo "O comando curl não foi encontrado." >&2; exit 2; }
is_zip() {
  "$python_bin" -c 'import sys, zipfile; raise SystemExit(0 if zipfile.is_zipfile(sys.argv[1]) else 1)' "$1" >/dev/null 2>&1
}

drive_id=""
if [[ "$source_url" =~ drive\.google\.com ]]; then
  if [[ "$source_url" =~ /d/([A-Za-z0-9_-]+) ]]; then
    drive_id="${BASH_REMATCH[1]}"
  elif [[ "$source_url" =~ [\?\&]id=([A-Za-z0-9_-]+) ]]; then
    drive_id="${BASH_REMATCH[1]}"
  fi
fi
download_url="$source_url"
if [[ -n "$drive_id" ]]; then
  download_url="https://drive.google.com/uc?export=download&id=$drive_id"
fi

data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
app_root="$data_home/puc-central"
bin_dir="$HOME/.local/bin"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
archive="$work_dir/source.zip"
cookie="$work_dir/cookies.txt"

curl --fail --location --silent --show-error --max-filesize 524288000 --cookie-jar "$cookie" --cookie "$cookie" "$download_url" -o "$archive"
if ! is_zip "$archive" && [[ -n "$drive_id" ]]; then
  confirmation="$(grep -Eo 'confirm=[0-9A-Za-z_-]+' "$archive" | head -n1 | cut -d= -f2 || true)"
  if [[ -n "$confirmation" ]]; then
    curl --fail --location --silent --show-error --max-filesize 524288000 --cookie-jar "$cookie" --cookie "$cookie" \
      "https://drive.google.com/uc?export=download&id=$drive_id&confirm=$confirmation" -o "$archive"
  fi
fi
if ! is_zip "$archive"; then
  echo "O download não parece ser um ZIP público válido. Confira o compartilhamento do arquivo no Drive." >&2
  exit 1
fi

mkdir -p "$work_dir/extracted" "$app_root"
"$python_bin" - "$archive" "$work_dir/extracted" <<'PY'
import sys
import zipfile
from pathlib import Path

archive, destination = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
if archive.stat().st_size > 500_000_000:
    raise SystemExit("O ZIP excede o limite de download de 500 MB.")
total = 0
with zipfile.ZipFile(archive) as source:
    for item in source.infolist():
        target = (destination / item.filename).resolve()
        if not target.is_relative_to(destination):
            raise SystemExit("O ZIP contém um caminho inválido.")
        total += item.file_size
        if total > 1_500_000_000:
            raise SystemExit("O ZIP excede o limite de extração de 1,5 GB.")
    source.extractall(destination)
PY
project_file="$(find "$work_dir/extracted" -type f -name puc.py -print -quit)"
if [[ -z "$project_file" ]]; then
  echo "Não encontrei puc.py no arquivo ZIP." >&2
  exit 1
fi
project_dir="$(dirname "$project_file")"
if [[ ! -f "$project_dir/requirements.txt" ]]; then
  echo "O ZIP não contém requirements.txt ao lado de puc.py." >&2
  exit 1
fi

new_source="$work_dir/source"
mkdir -p "$new_source"
cp -a "$project_dir/." "$new_source/"
rm -rf "$app_root/source.old"
if [[ -d "$app_root/source" ]]; then mv "$app_root/source" "$app_root/source.old"; fi
mv "$new_source" "$app_root/source"
rm -rf "$app_root/source.old"

if [[ ! -x "$app_root/venv/bin/python" ]]; then
  "$python_bin" -m venv "$app_root/venv"
fi
"$app_root/venv/bin/python" -m pip install --upgrade pip
"$app_root/venv/bin/python" -m pip install -r "$app_root/source/requirements.txt"

mkdir -p "$bin_dir"
cat > "$bin_dir/puc" <<'LAUNCHER'
#!/usr/bin/env bash
set -e
PUC_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/puc-central"
export PUC_DOWNLOADS_ROOT="$PUC_HOME/data/downloads_canvas"
export PUC_COURSES_CACHE="$PUC_HOME/data/cursos.json"
cd "$PUC_HOME/source"
exec "$PUC_HOME/venv/bin/python" "$PUC_HOME/source/puc.py" "$@"
LAUNCHER
chmod 755 "$bin_dir/puc"
if [[ ":$PATH:" != *":$bin_dir:"* ]]; then
  profile="$HOME/.profile"
  touch "$profile"
  if ! grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$profile"; then
    printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$profile"
  fi
fi

echo "Instalação concluída. Abra um terminal novo e rode: puc"
echo "Para conferir o ambiente: puc debug"
echo "A interface local abre em http://127.0.0.1:8091."
