#!/usr/bin/env bash
# Automatic installer for the LM Studio GPU bridge used by LM Chat.
#
# Run on Ubuntu/Debian:
#   sudo bash install.sh
#
# The bridge lets the Android app load models with an explicit GPU offload
# ratio through the `lms` CLI and keeps chat generation running on the server.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/lm-gpu-bridge"
ENV_FILE="/etc/lm-gpu-bridge.env"
UNIT_FILE="/etc/systemd/system/lms-gpu-bridge.service"
SERVICE_NAME="lms-gpu-bridge"
BRIDGE_SOURCE="$SCRIPT_DIR/lms_gpu_bridge.py"

BRIDGE_HOST="0.0.0.0"
BRIDGE_PORT="1235"
BRIDGE_TOKEN=""
LMS_BIN=""
LMS_HOST=""
LMS_API_KEY=""
TARGET_USER=""
ALLOW_FIREWALL=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Установка мостика LM Studio GPU для LM Chat.

Использование:
  sudo bash install.sh [параметры]

Параметры:
  --user USER          Пользователь, от которого работает LM Studio
                       (по умолчанию SUDO_USER)
  --token TOKEN        Токен мостика. Если не указан, будет создан случайный
  --port PORT          Порт мостика (по умолчанию 1235)
  --host ADDRESS       Адрес прослушивания (по умолчанию 0.0.0.0;
                       используйте 127.0.0.1 для доступа только с этой машины)
  --lms-bin PATH       Путь к CLI lms
  --lms-host HOST:PORT LM Studio на другом компьютере (добавляет --host)
  --lms-api-key KEY    API-токен LM Studio, если включена авторизация
  --allow-firewall     Выполнить `ufw allow PORT/tcp`, если ufw активен
  -h, --help           Показать эту справку

Примеры:
  sudo bash install.sh
  sudo bash install.sh --user spe4naz --port 1235
  sudo bash install.sh --lms-host 192.168.1.50:1234
  sudo bash install.sh --host 127.0.0.1
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)          TARGET_USER="${2:?--user требует значение}"; shift 2 ;;
    --token)         BRIDGE_TOKEN="${2:?--token требует значение}"; shift 2 ;;
    --port)          BRIDGE_PORT="${2:?--port требует значение}"; shift 2 ;;
    --host)          BRIDGE_HOST="${2:?--host требует значение}"; shift 2 ;;
    --lms-bin)       LMS_BIN="${2:?--lms-bin требует значение}"; shift 2 ;;
    --lms-host)      LMS_HOST="${2:?--lms-host требует значение}"; shift 2 ;;
    --lms-api-key)   LMS_API_KEY="${2:?--lms-api-key требует значение}"; shift 2 ;;
    --allow-firewall) ALLOW_FIREWALL=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) die "Неизвестный параметр: $1 (см. --help)" ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || die "Нужны права root: запустите через sudo."
  exec sudo -- bash "$0" "$@"
fi

# ---------------------------------------------------------------- checks

TEMP_BRIDGE=""
cleanup() {
  if [[ -n "$TEMP_BRIDGE" ]]; then
    rm -f "$TEMP_BRIDGE"
  fi
}
trap cleanup EXIT

if [[ ! -f "$BRIDGE_SOURCE" ]]; then
  BRIDGE_URL="https://raw.githubusercontent.com/Spe4naz/lm-chat-bridge-installer/main/lms_gpu_bridge.py"
  TEMP_BRIDGE="$(mktemp)"
  log "lms_gpu_bridge.py не найден рядом со скриптом, скачиваю из репозитория"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$BRIDGE_URL" -o "$TEMP_BRIDGE" || die "Не удалось скачать $BRIDGE_URL"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TEMP_BRIDGE" "$BRIDGE_URL" || die "Не удалось скачать $BRIDGE_URL"
  else
    die "Нет curl/wget. Скачайте lms_gpu_bridge.py вручную рядом со скриптом."
  fi
  BRIDGE_SOURCE="$TEMP_BRIDGE"
fi

command -v systemctl >/dev/null 2>&1 || die "systemd не найден."
command -v python3 >/dev/null 2>&1 || die "python3 не найден. Установите: sudo apt install python3"
PYTHON_BIN="$(command -v python3)"
python3 - <<'PY' || die "Нужен Python 3.8 или новее."
import sys
raise SystemExit(0 if sys.version_info >= (3, 8) else 1)
PY

if ! [[ "$BRIDGE_PORT" =~ ^[0-9]+$ ]] || (( BRIDGE_PORT < 1 || BRIDGE_PORT > 65535 )); then
  die "Некорректный порт: $BRIDGE_PORT"
fi

if [[ -z "$TARGET_USER" ]]; then
  TARGET_USER="${SUDO_USER:-}"
fi
if [[ -z "$TARGET_USER" || "$TARGET_USER" == "root" ]]; then
  die "Укажите пользователя: --user ИМЯ (от него работает LM Studio)."
fi
id "$TARGET_USER" >/dev/null 2>&1 || die "Пользователь не найден: $TARGET_USER"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" ]] || die "Не удалось определить домашний каталог $TARGET_USER"

if [[ -z "$LMS_BIN" && -x "$TARGET_HOME/.lmstudio/bin/lms" ]]; then
  LMS_BIN="$TARGET_HOME/.lmstudio/bin/lms"
fi
if [[ -z "$LMS_BIN" ]]; then
  LMS_BIN="$(command -v lms 2>/dev/null || true)"
fi
if [[ -n "$LMS_BIN" ]] && command -v readlink >/dev/null 2>&1; then
  LMS_BIN="$(readlink -f "$LMS_BIN" 2>/dev/null || printf '%s' "$LMS_BIN")"
fi
[[ -n "$LMS_BIN" && -x "$LMS_BIN" ]] || die \
  "CLI lms не найден. Укажите путь: --lms-bin $TARGET_HOME/.lmstudio/bin/lms"
if ! sudo -u "$TARGET_USER" "$LMS_BIN" --version >/dev/null 2>&1; then
  warn "Не удалось выполнить '$LMS_BIN --version' от $TARGET_USER. Проверьте установку LM Studio."
fi

if [[ -z "$BRIDGE_TOKEN" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    BRIDGE_TOKEN="$(openssl rand -hex 32)"
  else
    BRIDGE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  fi
  log "Сгенерирован случайный BRIDGE_TOKEN."
fi

# ---------------------------------------------------------------- install

log "Устанавливаю мостик в $INSTALL_DIR"
install -d -m 755 "$INSTALL_DIR"
install -m 755 "$BRIDGE_SOURCE" "$INSTALL_DIR/lms_gpu_bridge.py"

env_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

log "Записываю $ENV_FILE"
umask 077
{
  printf 'BRIDGE_TOKEN=%s\n' "$(env_quote "$BRIDGE_TOKEN")"
  printf 'BRIDGE_HOST=%s\n' "$(env_quote "$BRIDGE_HOST")"
  printf 'BRIDGE_PORT=%s\n' "$(env_quote "$BRIDGE_PORT")"
  printf 'LMS_BIN=%s\n' "$(env_quote "$LMS_BIN")"
  if [[ -n "$LMS_HOST" ]]; then
    printf 'LMS_HOST=%s\n' "$(env_quote "$LMS_HOST")"
  fi
  if [[ -n "$LMS_API_KEY" ]]; then
    printf 'LMS_API_KEY=%s\n' "$(env_quote "$LMS_API_KEY")"
  fi
  :
} > "$ENV_FILE"
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"

log "Создаю systemd-юнит $UNIT_FILE"
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=LM Studio GPU offload bridge for LM Chat
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON_BIN $INSTALL_DIR/lms_gpu_bridge.py
Restart=on-failure
RestartSec=3

NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

log "Запускаю сервис"
systemctl daemon-reload
if ! systemctl enable --now "$SERVICE_NAME"; then
  systemctl status "$SERVICE_NAME" --no-pager || true
  journalctl -u "$SERVICE_NAME" -n 80 --no-pager || true
  die "Не удалось запустить $SERVICE_NAME. Исправьте ошибку и повторите установку."
fi

if [[ "$ALLOW_FIREWALL" -eq 1 ]] && command -v ufw >/dev/null 2>&1; then
  if ufw status 2>/dev/null | grep -q '^Status: active'; then
    log "Открываю порт $BRIDGE_PORT/tcp в ufw"
    ufw allow "$BRIDGE_PORT/tcp"
  else
    warn "ufw не активен — порт не менялся."
  fi
fi

# ---------------------------------------------------------------- checks

log "Проверяю /health"
HEALTH_OK=0
for _ in $(seq 1 20); do
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 2 "http://127.0.0.1:$BRIDGE_PORT/health" >/dev/null; then
      HEALTH_OK=1
      break
    fi
  else
    if python3 - "$BRIDGE_PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
port = sys.argv[1]
urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).read()
PY
    then
      HEALTH_OK=1
      break
    fi
  fi
  sleep 1
done

LAN_IP="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -n1 || true)"
[[ -n "$LAN_IP" ]] || LAN_IP="IP-КОМПЬЮТЕРА"

echo
if [[ "$HEALTH_OK" -eq 1 ]]; then
  echo "Мостик установлен и работает."
else
  warn "Сервис запущен, но /health не ответил. Посмотрите: journalctl -u $SERVICE_NAME -n 80 --no-pager"
fi

cat <<EOF

Настройки для LM Chat:
  Адрес LM Studio : http://$LAN_IP:1234
  Адрес мостика   : http://$LAN_IP:$BRIDGE_PORT
  Токен мостика   : $BRIDGE_TOKEN
  Чат через мостик: включён

Проверка с телефона:
  http://$LAN_IP:$BRIDGE_PORT/health

Если телефон не подключается:
  - телефон и ПК должны быть в одной сети;
  - BRIDGE_HOST=$BRIDGE_HOST;
  - откройте порт $BRIDGE_PORT/tcp: sudo ufw allow $BRIDGE_PORT/tcp;
  - не открывайте порт в интернет, используйте Tailscale/VPN.

Управление:
  sudo systemctl status $SERVICE_NAME
  sudo journalctl -u $SERVICE_NAME -f
  sudo systemctl restart $SERVICE_NAME
EOF

if [[ "$BRIDGE_HOST" == "127.0.0.1" || "$BRIDGE_HOST" == "localhost" ]]; then
  warn "Мостик слушает только loopback. С телефона он будет недоступен."
fi
