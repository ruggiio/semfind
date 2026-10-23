#!/usr/bin/env bash
# Installa semfind per l'utente corrente (niente root). Rilanciabile: salta ciò che c'è già.
#
#   ./install.sh                     installa e avvia i servizi
#   ./install.sh --bind-super-space  assegna anche Super+Spazio a Spotlight (GNOME)
#   ./install.sh --cpu-torch         torch solo CPU (senza GPU NVIDIA; indicizzazione molto più lenta)
#   ./install.sh --no-services       installa ma non avvia nulla
set -euo pipefail

PREFIX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/semfind"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
UNITS="$CONF/systemd/user"
BIN="$HOME/.local/bin"
DBUS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/dbus-1/services"

LLAMA_VER="b11829"   # build di llama.cpp verificata con EmbeddingGemma 2
LLAMA_URL="https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_VER/llama-$LLAMA_VER-bin-ubuntu-x64.tar.gz"
GGUF_REPO="ggml-org/embeddinggemma-2-GGUF"
GGUF_FILE="embeddinggemma-2-Q8_0.gguf"
MODEL_ID="google/embeddinggemma-2"

CPU_TORCH=0 BIND=0 SERVICES=1
for a in "$@"; do
  case "$a" in
    --cpu-torch) CPU_TORCH=1 ;;
    --bind-super-space) BIND=1 ;;
    --no-services) SERVICES=0 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "opzione sconosciuta: $a" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merrore:\033[0m %s\n' "$*" >&2; exit 1; }

# sed su un template -> file vero. rm prima: se la destinazione è un symlink al template,
# scriverci dentro sovrascriverebbe il template stesso.
render() {
  rm -f "$2"
  sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@DATA@|$DATA|g" "$1" > "$2"
}

step "Controllo dipendenze"
command -v python3 >/dev/null || die "serve python3"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "serve Python ≥ 3.11"
for c in curl tar gdbus systemctl; do command -v "$c" >/dev/null || die "manca $c"; done
python3 - <<'PY' || die "servono GTK4 e libadwaita per Python: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
PY
[ "$(uname -m)" = x86_64 ] || die "il llama.cpp precompilato scaricato è per x86_64"
if [ "$CPU_TORCH" = 0 ] && ! command -v nvidia-smi >/dev/null; then
  echo "nessuna GPU NVIDIA trovata: uso torch solo CPU (l'indicizzazione sarà ~15 volte più lenta)"
  CPU_TORCH=1
fi
command -v sushi >/dev/null || [ -x /usr/libexec/org.gnome.NautilusPreviewer ] \
  || echo "facoltativo: sudo apt install gnome-sushi  (anteprima rapida con Ctrl+Y)"

step "Ambiente Python dell'indicizzatore (.venv, alcuni GB)"
[ -x "$PREFIX/.venv/bin/python" ] || python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install -q --upgrade pip
if [ "$CPU_TORCH" = 1 ]; then
  "$PREFIX/.venv/bin/pip" install -q torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cpu
fi
"$PREFIX/.venv/bin/pip" install -q -r "$PREFIX/requirements.txt"

step "Ambiente Python di Spotlight (.venv-ui, usa GTK di sistema)"
[ -x "$PREFIX/.venv-ui/bin/python" ] || python3 -m venv --system-site-packages "$PREFIX/.venv-ui"
"$PREFIX/.venv-ui/bin/pip" install -q sqlite-vec==0.1.9

step "llama.cpp $LLAMA_VER"
if [ ! -x "$PREFIX/vendor/llama-$LLAMA_VER/llama-server" ]; then
  mkdir -p "$PREFIX/vendor"
  tmp="$(mktemp -d)"
  curl -fL --progress-bar "$LLAMA_URL" -o "$tmp/llama.tar.gz"
  tar -xzf "$tmp/llama.tar.gz" -C "$tmp"
  rm -rf "$PREFIX/vendor/llama-$LLAMA_VER"
  mv "$tmp/llama-$LLAMA_VER" "$PREFIX/vendor/llama-$LLAMA_VER"
  rm -rf "$tmp"
fi
ln -sfn "llama-$LLAMA_VER" "$PREFIX/vendor/llama"

step "Modelli (EmbeddingGemma 2: 1,5 GB per l'indicizzazione, 310 MB GGUF per le query)"
mkdir -p "$DATA"
GGUF_PATH="$("$PREFIX/.venv/bin/python" -c "
from huggingface_hub import hf_hub_download, snapshot_download
snapshot_download('$MODEL_ID')
print(hf_hub_download('$GGUF_REPO', '$GGUF_FILE'))" | tail -1)"
ln -sf "$GGUF_PATH" "$DATA/$GGUF_FILE"

step "Comandi, servizi utente, attivazione DBus"
mkdir -p "$BIN" "$UNITS" "$DBUS_DIR" "$CONF/semfind"
for f in "$PREFIX"/bin/*; do render "$f" "$BIN/$(basename "$f")"; chmod +x "$BIN/$(basename "$f")"; done
for f in "$PREFIX"/systemd/*; do render "$f" "$UNITS/$(basename "$f")"; done
render "$PREFIX/dbus/io.github.semfind.Spotlight.service" "$DBUS_DIR/io.github.semfind.Spotlight.service"
[ -f "$CONF/semfind/config.toml" ] || cp "$PREFIX/config.example.toml" "$CONF/semfind/config.toml"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "attenzione: $BIN non è nel PATH" ;; esac

if [ "$SERVICES" = 1 ]; then
  step "Avvio dei servizi"
  systemctl --user daemon-reload
  systemctl --user enable semfind-embed.service semfind-spotlight.service semfind-watch.service semfind-index.timer
  systemctl --user restart semfind-embed.service
  systemctl --user restart semfind-spotlight.service semfind-watch.service
  systemctl --user start semfind-index.timer
fi

if [ "$BIND" = 1 ]; then
  step "Scorciatoia Super+Spazio"
  KB=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/semfind-spotlight/
  list="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)"
  if [[ "$list" != *"$KB"* ]]; then
    if [[ "$list" == "@as []" || "$list" == "[]" ]]; then list="['$KB']"; else list="${list%]}, '$KB']"; fi
    gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$list"
  fi
  S="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KB"
  gsettings set "$S" name 'Spotlight (semfind)'
  gsettings set "$S" command "$BIN/semfind-spotlight-toggle"
  gsettings set "$S" binding '<Super>space'
  # Super+Spazio è il cambio layout di tastiera: lo tolgo solo se c'è un unico layout
  if [ "$(gsettings get org.gnome.desktop.input-sources sources | grep -o "('" | wc -l)" -le 1 ]; then
    gsettings set org.gnome.desktop.wm.keybindings switch-input-source "['XF86Keyboard']"
  else
    echo "hai più layout di tastiera: Super+Spazio li cambia ancora; scegli un'altra scorciatoia in Impostazioni → Tastiera"
  fi
fi

step "Fatto"
cat <<EOF
Primo indice (meglio sotto carica, con la GPU ci vuole circa un'ora ogni ~60.000 pezzi di testo):
    semfind index
Poi:
    cerca "la tua domanda"          dal terminale
    Super+Spazio                    Spotlight (con --bind-super-space, oppure assegna
                                    $BIN/semfind-spotlight-toggle a una scorciatoia)
Configurazione: $CONF/semfind/config.toml
EOF
