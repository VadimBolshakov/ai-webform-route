# Деплой на VPS

Инструкция для первого развёртывания на чистом сервере Ubuntu 24.04 с доменом `aramistrio.online`.
Схема та же, что локально: `db` (PostgreSQL) + `app` (FastAPI) + `caddy` (HTTPS) в Docker Compose.
На сервере Caddy сам получает и продлевает сертификат Let's Encrypt — отдельный nginx не нужен.

```
Интернет ──80/443──► caddy (Let's Encrypt, лимит тела 60 МБ) ──► app:8000 ──► db:5432
                                                                    │
                                                         OpenRouter API (HTTPS наружу)
```

Отличия от локального запуска задаёт файл `docker-compose.prod.yml`:
порт Postgres наружу не публикуется, Caddy слушает 80 и 443 и использует `docker/Caddyfile.prod`.

> Этап 2 ещё не реализован: Telegram-бот и дайджесты, бэкапы через restic по расписанию, CI.
> Ниже есть минимальный ручной бэкап — настройте его сразу.

## 0. Что понадобится

- VPS: 2 vCPU / 4 ГБ RAM / 40 ГБ SSD, Ubuntu 24.04, Германия или Нидерланды (см. раздел «Провайдеры»).
- Доступ к DNS домена `aramistrio.online`.
- Репозиторий на GitHub — **приватный** (в нём нет секретов, но незачем светить устройство сайта).
- SSH-ключ на вашем компьютере (`ssh-keygen -t ed25519`, если его ещё нет).
- Ключ OpenRouter с жёстким лимитом (см. README, раздел «Ручные шаги в кабинете OpenRouter»).

## 1. DNS

У регистратора домена создайте (или измените) запись:

| Тип | Имя | Значение | TTL |
|---|---|---|---|
| A | `@` | IPv4 нового сервера | 300 |
| AAAA | `@` | IPv6 нового сервера (если есть) | 300 |

Если сейчас `aramistrio.online` указывает на сервер с 3X-ui, после смены записи домен уйдёт на новый сервер.
Если 3X-ui использует этот домен (сертификат, SNI), заранее переведите его на поддомен или IP.

Проверка (с вашего компьютера, через несколько минут):

```bash
nslookup aramistrio.online
```

## 2. Базовая настройка сервера

Подключитесь под root (данные пришлёт провайдер) и создайте своего пользователя:

```bash
adduser vadim
usermod -aG sudo vadim
mkdir -p /home/vadim/.ssh && cp ~/.ssh/authorized_keys /home/vadim/.ssh/ 2>/dev/null; chown -R vadim:vadim /home/vadim/.ssh
```

Если провайдер не добавил ваш ключ при создании сервера, с вашего компьютера:

```bash
ssh-copy-id vadim@<IP>
```

Проверьте, что вход по ключу работает (`ssh vadim@<IP>`), и только потом закройте вход по паролю и под root.
Создайте файл `/etc/ssh/sshd_config.d/10-hardening.conf`:

```
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
```

```bash
sudo systemctl restart ssh
```

Обновления, файрвол, fail2ban:

```bash
sudo apt update && sudo apt -y full-upgrade
sudo apt -y install ufw fail2ban unattended-upgrades git
sudo dpkg-reconfigure -plow unattended-upgrades   # ответить Yes
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo systemctl enable --now fail2ban               # по умолчанию защищает sshd
```

> Docker публикует порты в обход ufw. Поэтому наружу в compose опубликованы только 80 и 443 (Caddy);
> Postgres и приложение снаружи недоступны независимо от ufw.

Swap (на 4 ГБ не обязателен, но страхует от OOM при сборке образа):

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 3. Docker

Официальный репозиторий Docker (не пакет `docker.io` из Ubuntu — нужен свежий Compose):

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker vadim    # перелогиньтесь после этого
docker compose version
```

## 4. Код с GitHub (deploy key)

На сервере создайте отдельный ключ только для чтения репозитория:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

В GitHub: репозиторий → Settings → Deploy keys → Add deploy key, вставьте ключ, **без** галочки «Allow write access».
Затем на сервере:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
sudo mkdir -p /opt/aichat && sudo chown vadim:vadim /opt/aichat
git clone git@github.com:<ваш-логин>/<репозиторий>.git /opt/aichat
cd /opt/aichat
```

## 5. Секреты (.env)

Секреты генерируются и живут **только на сервере** (не в Dropbox и не в git):

```bash
cd /opt/aichat
cp .env.example .env
chmod 600 .env
python3 -c "import secrets; print('LOGIN_PATH_TOKEN=' + secrets.token_urlsafe(24))"
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
python3 -c "import base64, os; print('FERNET_KEY=' + base64.urlsafe_b64encode(os.urandom(32)).decode())"
nano .env
```

Что поменять в `.env`:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
DOMAIN=aramistrio.online
ACME_EMAIL=<ваша почта>
BASE_URL=https://aramistrio.online
LOGIN_PATH_TOKEN=<сгенерированный>
FERNET_KEY=<сгенерированный>
POSTGRES_PASSWORD=<сгенерированный>
DATABASE_URL=postgresql+asyncpg://aichat:<тот же POSTGRES_PASSWORD>@db:5432/aichat
OPENROUTER_API_KEY=<ключ с лимитом>
AUTO_ALLOWED_MODELS=["openai/gpt-4o-mini","anthropic/claude-haiku-*","google/gemini-*-flash*"]
DEFAULT_MONTHLY_LIMIT_USD=5
```

- `COMPOSE_FILE` позволяет дальше писать просто `docker compose ...` — прод-переопределения подключатся сами.
- `POSTGRES_PASSWORD` применяется только при **первом** создании базы. Менять его потом — через `ALTER USER` в psql.
- `FERNET_KEY` шифрует секреты 2FA. Потеряете ключ — всем придётся заново настраивать 2FA. Сохраните копию `.env` в менеджере паролей.
- `AUTO_ALLOWED_MODELS` — проверьте актуальные id моделей на openrouter.ai/models.

## 6. Первый запуск

```bash
cd /opt/aichat
docker compose up -d --build
docker compose ps                   # db, app, caddy → healthy
docker compose logs caddy | grep -i certificate   # должна быть строка об успешном получении сертификата
```

Создайте администратора и сразу настройте 2FA:

```bash
docker compose exec app python -m app.cli create-admin vadim
```

Откройте `https://aramistrio.online/a/<LOGIN_PATH_TOKEN>`, войдите — приложение потребует настроить 2FA
(Google Authenticator / Aegis). Затем: Админка → добавьте 3–6 разрешённых моделей → создайте инвайт и проверьте
регистрацию в другом браузере → отправьте тестовое сообщение (это первый реальный запрос к OpenRouter).

Проверки снаружи (с вашего компьютера):

```bash
curl -sI https://aramistrio.online/ | head -20     # 200, заголовки Strict-Transport-Security, Content-Security-Policy
curl -s -o /dev/null -w "%{http_code}\n" https://aramistrio.online/healthz   # 404 — так и должно быть
curl -s -o /dev/null -w "%{http_code}\n" https://aramistrio.online/app       # 404 без входа
```

И онлайн: [SSL Labs](https://www.ssllabs.com/ssltest/) и [Mozilla Observatory](https://developer.mozilla.org/en-US/observatory) для `aramistrio.online`.

## 7. Обновление

```bash
cd /opt/aichat
git pull
docker compose up -d --build        # миграции применяются сами при старте app
docker image prune -f
```

Перед обновлением с миграциями сделайте бэкап (раздел 8).

## 8. Бэкап (минимальный, до этапа 2)

Скрипт `/opt/aichat-backup.sh`:

```bash
#!/bin/sh
set -e
mkdir -p /var/backups/aichat
cd /opt/aichat
docker compose exec -T db pg_dump -U aichat -Fc aichat > /var/backups/aichat/aichat-$(date +%F).dump
find /var/backups/aichat -name '*.dump' -mtime +7 -delete
```

```bash
sudo chmod 700 /opt/aichat-backup.sh
sudo chmod 700 /var/backups 2>/dev/null; sudo mkdir -p /var/backups/aichat && sudo chmod 700 /var/backups/aichat
echo '30 3 * * * root /opt/aichat-backup.sh' | sudo tee /etc/cron.d/aichat-backup
sudo /opt/aichat-backup.sh && ls -lh /var/backups/aichat
```

Это копия **на том же сервере** — от сбоя диска или взлома она не спасёт. Пока нет этапа 2 (restic → S3 с шифрованием),
раз в неделю забирайте последний дамп к себе и храните зашифрованным:

```bash
scp vadim@aramistrio.online:/var/backups/aichat/aichat-$(date +%F).dump .
```

(файл принадлежит root — либо копируйте через `sudo cp` в домашнюю папку, либо запускайте cron от `vadim`, добавив его в группу `docker`).

Восстановление (проверьте хотя бы раз):

```bash
docker compose exec -T db pg_restore -U aichat -d aichat --clean --if-exists < aichat-ГГГГ-ММ-ДД.dump
docker compose restart app
```

## 9. Эксплуатация

- Логи: `docker compose logs -f app`. Access-логи выключены намеренно (в путях токены входа и инвайтов).
- Uvicorn работает в одном процессе — не добавляйте `--workers` (лимиты и планировщик в памяти процесса).
- Доступ админа потерян: `docker compose exec app python -m app.cli reset-2fa vadim` или `reset-password vadim`.
- Перезапуск сервера: контейнеры поднимутся сами (`restart: unless-stopped`).
- Место на диске: `docker system df`, `df -h`.

## Провайдеры

Требования: Германия/Нидерланды, 2 vCPU / 4 ГБ / 40 ГБ, IPv4, KVM, почасовая или помесячная оплата, ~€4–8 в месяц.

| Провайдер | Плюсы | Минусы |
|---|---|---|
| **Hetzner Cloud** (CX-линейка, Фалькенштайн/Нюрнберг) | Лучшее соотношение цена/качество, быстрые NVMe, понятная панель, снапшоты и бэкапы в один клик, собственный S3 (Object Storage) для бэкапов этапа 2 | Строгая верификация при регистрации; оплата картой РФ не пройдёт |
| **Netcup** (VPS / Root Server, Нюрнберг) | Очень дёшево за ресурсы, стабильно | Устаревшая панель, минимальный срок аренды на некоторых тарифах, верификация |
| **DigitalOcean / Vultr** (Франкфурт, Амстердам) | Отличная документация, снапшоты, быстрый старт | Дороже аналогов в 1,5–2 раза |
| **Aeza, VDSina, Timeweb Cloud** (локации в NL/DE) | Принимают оплату из РФ (в т. ч. СБП, криптовалюта) | Компании под юрисдикцией РФ (запросы данных); поддержка и стабильность заметно разнятся |

Если оплата иностранной картой доступна — Hetzner Cloud. Если нет — смотрите, где сейчас ваш текущий VPS с 3X-ui:
раз он уже работает и оплачивается, тот же провайдер с тарифом 2/4/40 — самый простой вариант.
В любом случае проверьте, что IP нового сервера не в блок-листах (`https://mxtoolbox.com/blacklists.aspx`)
и что `curl https://openrouter.ai/api/v1/models` с него отвечает.
