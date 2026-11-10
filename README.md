# LM Chat — GPU bridge installer

Автоматическая установка HTTP-мостика к `lms` для приложения **LM Chat**.

Мостик нужен, чтобы Android-клиент мог:

- загружать модель с явным распределением слоёв на GPU
  (`lms load --gpu max|off|0.5`);
- оценивать память до загрузки (`--estimate-only`);
- запускать чат как задание на сервере: ответ продолжает генерироваться,
  даже если телефон свернули, потеряли Wi-Fi или закрыли приложение.

Требуется Ubuntu/Debian с systemd, Python 3.8+ и установленный LM Studio с CLI
`lms`. Мостик использует только стандартную библиотеку Python.

## Быстрая установка

```bash
curl -fsSL https://raw.githubusercontent.com/Spe4naz/lm-chat-bridge-installer/main/install.sh -o /tmp/lm-bridge-install.sh
sudo bash /tmp/lm-bridge-install.sh --user "$USER"
```

Или из клонированного репозитория:

```bash
git clone https://github.com/Spe4naz/lm-chat-bridge-installer.git
cd lm-chat-bridge-installer
sudo bash install.sh --user "$USER"
```

Скрипт:

1. проверяет Python, systemd и `lms`;
2. копирует `lms_gpu_bridge.py` в `/opt/lm-gpu-bridge`;
3. создаёт `/etc/lm-gpu-bridge.env` с правами `600`;
4. создаёт и запускает systemd-сервис `lms-gpu-bridge`;
5. генерирует случайный `BRIDGE_TOKEN`, если он не передан;
6. проверяет `/health` и печатает настройки для приложения.

После установки в приложении LM Chat укажите:

- **Сервер:** `http://IP-КОМПЬЮТЕРА:1234` — без `/v1`;
- **Адрес мостика:** `http://IP-КОМПЬЮТЕРА:1235`;
- **Токен мостика:** значение, которое напечатал установщик;
- включите **«Чат через мостик»**.

## Параметры

| Параметр | Назначение |
|---|---|
| `--user USER` | пользователь, от которого работает LM Studio; по умолчанию `SUDO_USER` |
| `--token TOKEN` | свой токен мостика; иначе будет сгенерирован |
| `--port PORT` | порт мостика, по умолчанию `1235` |
| `--host ADDRESS` | адрес прослушивания, по умолчанию `0.0.0.0` |
| `--lms-bin PATH` | путь к CLI `lms` |
| `--lms-host HOST:PORT` | LM Studio на другом компьютере |
| `--lms-api-key KEY` | API-токен LM Studio, если включена авторизация |
| `--allow-firewall` | выполнить `ufw allow PORT/tcp`, если ufw активен |
| `--help` | справка |

Примеры:

```bash
# обычная установка
sudo bash install.sh --user "$USER"

# другой порт и свой токен
sudo bash install.sh --user "$USER" --port 1236 --token 'длинный-токен'

# LM Studio на другом компьютере
sudo bash install.sh --user "$USER" --lms-host 192.168.1.50:1234

# только с этой машины (с телефона будет недоступно)
sudo bash install.sh --user "$USER" --host 127.0.0.1
```

## Ручная проверка

```bash
curl http://127.0.0.1:1235/health

curl -X POST http://127.0.0.1:1235/load \
  -H "Authorization: Bearer ВАШ_ТОКЕН" \
  -H "Content-Type: application/json" \
  -d '{"model":"google/gemma-3n-e4b","gpu":"max","context_length":8192}'
```

Оценка памяти без загрузки:

```bash
curl -X POST http://127.0.0.1:1235/estimate \
  -H "Authorization: Bearer ВАШ_ТОКЕН" \
  -H "Content-Type: application/json" \
  -d '{"model":"google/gemma-3n-e4b","gpu":"max","context_length":8192}'
```

## Управление

```bash
sudo systemctl status lms-gpu-bridge
sudo journalctl -u lms-gpu-bridge -f
sudo systemctl restart lms-gpu-bridge
sudo systemctl disable --now lms-gpu-bridge
```

Файлы:

- `/opt/lm-gpu-bridge/lms_gpu_bridge.py` — сам мостик;
- `/etc/lm-gpu-bridge.env` — токен и параметры, права `600`;
- `/etc/systemd/system/lms-gpu-bridge.service` — systemd-юнит.

## Безопасность

- `BRIDGE_TOKEN` обязателен для сетевого адреса. Без токена мостик
  отказывается слушать `0.0.0.0`.
- Не открывайте порт мостика в интернет. Для внешнего доступа используйте
  Tailscale/VPN.
- Токен хранится в `/etc/lm-gpu-bridge.env` с правами `600`.
- Для локальной проверки можно поставить `--host 127.0.0.1`.

## Если что-то не работает

| Симптом | Что проверить |
|---|---|
| `lms не найден` | `--lms-bin /home/USER/.lmstudio/bin/lms` |
| `/health` не отвечает | `sudo journalctl -u lms-gpu-bridge -n 100 --no-pager` |
| `401 unauthorized` | токен в приложении совпадает с `BRIDGE_TOKEN` |
| `404` на `/chat` | обновлён ли `lms_gpu_bridge.py` |
| Телефон не подключается | `BRIDGE_HOST`, `ufw allow 1235/tcp`, одна Wi-Fi сеть |
| Ошибка доступа к GPU | попробуйте убрать `PrivateDevices=true` из юнита |
| `OutOfMemory` при загрузке | уменьшите `context_length` или GPU-оффлоад |

Самопроверка мостика без LM Studio:

```bash
python3 test_lms_gpu_bridge.py
```
