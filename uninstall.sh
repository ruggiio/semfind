#!/usr/bin/env bash
# Disinstalla servizi, comandi e scorciatoia. L'indice resta, a meno di --purge.
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/semfind"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
UNITS="$CONF/systemd/user"

systemctl --user disable --now semfind-index.timer semfind-watch.service semfind-spotlight.service semfind-embed.service 2>/dev/null || true
rm -f "$UNITS"/semfind-{index.service,index.timer,embed.service,spotlight.service,watch.service}
systemctl --user daemon-reload
rm -f "$HOME/.local/bin"/{semfind,cerca,semfind-spotlight-toggle}
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/dbus-1/services/io.github.semfind.Spotlight.service"

KB=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/semfind-spotlight/
if command -v gsettings >/dev/null; then
  list="$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)"
  if [[ "$list" == *"$KB"* ]]; then
    list="$(echo "$list" | sed -e "s|, '$KB'||" -e "s|'$KB', ||" -e "s|'$KB'||")"
    gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$list"
    gsettings reset-recursively "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KB"
    echo "scorciatoia rimossa; per riavere Super+Spazio come cambio layout:"
    echo "  gsettings reset org.gnome.desktop.wm.keybindings switch-input-source"
  fi
fi

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$DATA" "$CONF/semfind"
  echo "indice e configurazione cancellati"
else
  echo "indice lasciato in $DATA (usa --purge per cancellarlo)"
fi
