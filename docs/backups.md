# Бэкапы базы в AWS S3

Как устроены бэкапы, как их один раз настроить (AWS, healthchecks.io, сервер) и как восстановиться.
Команды копируются как есть. **Подставлять нужно только значения в угловых скобках** — `<bucket>`,
`<ping-url>`, `<snapshot-id>` и т. п. — вместе со скобками: `<bucket>` → `aichat-backup-7f3k2`.

- Команды в блоках `bash` выполняются **на сервере** (по SSH, под пользователем `vadim`).
- Команды в блоках `powershell` выполняются **на вашем компьютере** в PowerShell, в папке репозитория
  `C:\Users\Vadim\Dropbox\AI_WebForm_Route`.

## 1. Что и как бэкапится

```
 каждую ночь 03:30 (время сервера)           1-го числа 04:30
 systemd: aichat-backup.timer                systemd: aichat-restore-test.timer
            │                                            │
            ▼                                            ▼
      backup.sh                                   restore-test.sh
            │ pg_dump (сжатый дамп,                      │ restic dump latest
            │ на диск не пишется)                        ▼
            ▼                                     временная БД restore_test:
   restic (в Docker) ── шифрует паролем ──┐        users ≥ 1, alembic_version,
            │                             │        снапшот не старше 48 ч → БД удаляется
            ▼                             ▼                      │
   AWS S3, eu-central-1: бакет <bucket>, папка aichat/            │
   версии объектов хранятся 30 дней (защита от удаления)          │
            │                                                     │
            └──── пинги start / ok / fail ──► healthchecks.io ◄───┘
                                                   │ нет пинга или fail
                                                   ▼
                                              письмо вам
```

- Каждую ночь: дамп базы → новый **снапшот** в репозитории restic. Хранятся последние 7 ежедневных,
  4 еженедельных и 3 ежемесячных снапшота (старые удаляются сами). По воскресеньям (по UTC) после бэкапа —
  `restic check` (проверка целостности репозитория).
- Раз в месяц: последний снапшот восстанавливается во временную базу и проверяется — так видно,
  что из бэкапа действительно можно восстановиться. Если снапшоту больше 48 часов — это тоже сбой
  (значит, ночные бэкапы перестали создаваться).
- Всё зашифровано паролем репозитория. **Без этого пароля бэкапы восстановить невозможно** — он хранится
  на сервере и у вас в менеджере паролей.
- Если что-то пошло не так, healthchecks.io пришлёт письмо.

**Остаточный риск.** Всё, что выше, проверяет сам сервер. Злоумышленник с правами root на сервере может
удалить снапшоты (`restic forget --prune`) и продолжать слать в healthchecks.io «всё хорошо» — письма
не будет. Удалённые данные хранятся в бакете ещё 30 дней (раздел 7), но только если вы заметите это
в эти 30 дней. Поэтому раз в месяц проверяйте снапшоты **со своего компьютера** — раздел 8.

Файлы на сервере:

| Путь | Что это |
|---|---|
| `/etc/aichat/restic.env` | адрес репозитория, ключ AWS сервера, URL пингов (секреты, права 600) |
| `/etc/aichat/restic-password` | пароль репозитория (секрет, права 600) |
| `/var/lib/aichat-backup/status.json` | итог последнего ночного бэкапа |
| `/var/lib/aichat-backup/restore-test.json` | итог последней проверки восстановления |
| `/var/cache/aichat-restic` | кэш restic (можно удалить, восстановится сам) |

## 2. AWS: бакет и ключ для сервера

Нужны: бакет S3 с версионированием и lifecycle, IAM-пользователь `aichat-backup-server` с ограниченными правами
и его ключ доступа (он попадёт на сервер). Сделайте это **один** раз: через веб-консоль (2.2) **или**
через AWS CLI (2.3) — результат одинаковый. AWS CLI на вашем компьютере (2.1) понадобится в любом
случае: для проверки прав (раздел 5) и для отката бакета после атаки (раздел 7).

Придумайте имя бакета: только маленькие латинские буквы, цифры и `-`, уникальное во всём AWS,
например `aichat-backup-` и несколько случайных букв. Дальше это `<bucket>`.

### 2.1 AWS CLI на вашем компьютере

1. Установите AWS CLI (PowerShell от администратора), затем откройте **новое** окно PowerShell:

   ```powershell
   msiexec.exe /i https://awscli.amazonaws.com/AWSCLIV2.msi
   aws --version
   ```

2. Ключ для CLI — от вашей учётной записи-администратора, **не** от root. Если такой ещё нет:
   консоль AWS → **IAM** → **Users** → **Create user** → имя `vadim-admin` → **Next** →
   **Attach policies directly** → отметьте `AdministratorAccess` → **Next** → **Create user**.
   Затем откройте пользователя `vadim-admin` → вкладка **Security credentials** → **Create access key** →
   **Command Line Interface (CLI)** → отметьте подтверждение → **Next** → **Create access key**.
   Окно с ключом не закрывайте до следующего шага.

3. Сохраните ключ в профиль `aichat-admin` (спросит Access Key ID, Secret Access Key, регион и формат):

   ```powershell
   aws configure --profile aichat-admin
   ```

   Ответы: ваш Access Key ID, ваш Secret Access Key, `eu-central-1`, `json`. Проверка:

   ```powershell
   aws sts get-caller-identity --profile aichat-admin
   ```

   Этот ключ даёт полный доступ к AWS: храните его только в профиле AWS CLI. Если компьютер
   скомпрометирован — удалите ключ в IAM (`vadim-admin` → Security credentials → Delete).

### 2.2 Через веб-консоль

**Бакет.** Консоль AWS → вверху справа регион **Europe (Frankfurt) eu-central-1** → **S3** → **Create bucket**:

- Bucket name: `<bucket>`;
- Object Ownership: **ACLs disabled (recommended)** (это и есть «Bucket owner enforced»);
- Block Public Access: **Block all public access** — отмечено;
- Bucket Versioning: **Enable**;
- Default encryption: **Server-side encryption with Amazon S3 managed keys (SSE-S3)**;
- **Create bucket**.

**Lifecycle.** Откройте бакет → вкладка **Management** → **Create lifecycle rule**. Нужно три правила
(для каждого: Rule scope — **Apply to all objects in the bucket** и галочка подтверждения):

| Rule name | Lifecycle rule actions | Значение |
|---|---|---|
| `expire-noncurrent-versions` | **Permanently delete noncurrent versions of objects** | Days after objects become noncurrent: `30` (поле «Number of newer versions to retain» — пустое) |
| `remove-orphan-delete-markers` | **Delete expired object delete markers or incomplete multipart uploads** → только **Delete expired object delete markers** | — |
| `abort-incomplete-multipart` | **Delete expired object delete markers or incomplete multipart uploads** → только **Delete incomplete multipart uploads** | Number of days: `7` |

Это то же, что в `ops/backup/aws/lifecycle.json`:

```json
{
  "Rules": [
    {
      "ID": "expire-noncurrent-versions",
      "Status": "Enabled",
      "Filter": {},
      "NoncurrentVersionExpiration": {
        "NoncurrentDays": 30
      }
    },
    {
      "ID": "remove-orphan-delete-markers",
      "Status": "Enabled",
      "Filter": {},
      "Expiration": {
        "ExpiredObjectDeleteMarker": true
      }
    },
    {
      "ID": "abort-incomplete-multipart",
      "Status": "Enabled",
      "Filter": {},
      "AbortIncompleteMultipartUpload": {
        "DaysAfterInitiation": 7
      }
    }
  ]
}
```

**IAM-пользователь сервера.** Сначала подготовьте политику с именем вашего бакета (на компьютере;
в первой строке замените `<bucket>`):

```powershell
$bucket = "<bucket>"
(Get-Content ops\backup\aws\iam-policy.json -Raw).Replace("BUCKET_NAME", $bucket) | Set-Content -Encoding ascii "$env:TEMP\aichat-iam-policy.json"
notepad "$env:TEMP\aichat-iam-policy.json"
```

Консоль AWS → **IAM** → **Users** → **Create user**:

- User name: `aichat-backup-server`; галочку **Provide user access to the AWS Management Console** **не** ставить;
- **Next** → Permissions options: **Attach policies directly**, ни одну политику **не** отмечать → **Next** → **Create user**;
- откройте пользователя → вкладка **Permissions** → **Add permissions** → **Create inline policy** →
  переключатель **JSON** → удалите всё в редакторе и вставьте содержимое открытого Блокнота (Ctrl+A, Ctrl+C) →
  **Next** → Policy name: `aichat-backup` → **Create policy**;
- вкладка **Security credentials** → **Create access key** → **Application running outside AWS** → **Next** →
  **Create access key**. Скопируйте **Access key** и **Secret access key** — они нужны в разделе 4
  (`AWS_ACCESS_KEY_ID` и `AWS_SECRET_ACCESS_KEY`). Секретную часть AWS больше не покажет; в файлы и почту
  её не сохраняйте — сразу на сервер (а копию — в менеджер паролей вместе с `restic.env`, см. раздел 4).

Политика разрешает серверу только то, что нужно restic (чтение, запись, удаление текущих объектов;
в бакете с версионированием удаление лишь прячет объект, старая версия остаётся). Отдельный явный
запрет (Deny) закрывает всё, что уничтожает историю: удаление версий (`s3:DeleteObjectVersion`),
выключение версионирования, изменение lifecycle, политики бакета и Object Lock, удаление бакета.
Поэтому даже с украденным ключом сервера старые версии проживут 30 дней, и бакет можно откатить (раздел 7).

**У пользователя `aichat-backup-server` должна быть только эта одна inline-политика**: никаких других
политик и групп. Лишнее разрешение (например, на IAM) позволило бы с ключом сервера снять запрет.
Проверка — в разделе 5.

### 2.3 Через AWS CLI (вместо 2.2)

В первой строке замените `<bucket>`:

```powershell
$bucket = "<bucket>"
$env:AWS_PROFILE = "aichat-admin"
aws s3api create-bucket --bucket $bucket --region eu-central-1 --create-bucket-configuration LocationConstraint=eu-central-1 --object-ownership BucketOwnerEnforced
aws s3api put-public-access-block --bucket $bucket --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
aws s3api put-bucket-versioning --bucket $bucket --versioning-configuration Status=Enabled
aws s3api put-bucket-lifecycle-configuration --bucket $bucket --lifecycle-configuration file://ops/backup/aws/lifecycle.json
aws s3api get-bucket-encryption --bucket $bucket
aws s3api get-bucket-versioning --bucket $bucket
```

Шифрование SSE-S3 AWS включает для новых бакетов сам: `get-bucket-encryption` должен показать
`"SSEAlgorithm": "AES256"`, `get-bucket-versioning` — `"Status": "Enabled"`.

IAM-пользователь сервера (без входа в консоль), его единственная inline-политика и ключ:

```powershell
(Get-Content ops\backup\aws\iam-policy.json -Raw).Replace("BUCKET_NAME", $bucket) | Set-Content -Encoding ascii "$env:TEMP\aichat-iam-policy.json"
aws iam create-user --user-name aichat-backup-server
aws iam put-user-policy --user-name aichat-backup-server --policy-name aichat-backup --policy-document "file://$env:TEMP\aichat-iam-policy.json"
aws iam create-access-key --user-name aichat-backup-server
```

Последняя команда печатает `AccessKeyId` и `SecretAccessKey` — это `AWS_ACCESS_KEY_ID` и
`AWS_SECRET_ACCESS_KEY` для раздела 4. Что разрешает и запрещает политика — см. конец 2.2.

## 3. healthchecks.io

1. Зарегистрируйтесь на <https://healthchecks.io> (бесплатный тариф). Письма о сбоях придут на почту
   аккаунта (Integrations → Email уже включён).
2. Узнайте часовой пояс сервера:

   ```bash
   timedatectl | grep "Time zone"
   ```

   Например, `Time zone: Etc/UTC (UTC, +0000)` — пояс `Etc/UTC`. Таймеры запускаются по этому времени,
   поэтому в проверках ниже нужен тот же пояс.
3. **Add Check** → откройте проверку → **Name**: `aichat-backup`. **Change Schedule…** → вкладка **Cron**:
   - Cron Expression: `30 3 * * *`;
   - Server's Time Zone: пояс из шага 2 (например, `Etc/UTC`);
   - Grace Time: `3 hours`;
   - **Save**.
4. Ещё одна проверка: **Name** `aichat-restore-test`, Cron `30 4 1 * *`, тот же пояс, Grace Time `1 day`.
5. На странице каждой проверки есть **Ping URL** вида `https://hc-ping.com/…`. Это `<ping-url>`:
   URL проверки `aichat-backup` → `HC_BACKUP_URL`, URL `aichat-restore-test` → `HC_RESTORE_URL` (раздел 4).
   URL — тоже секрет (по нему можно слать ложные «всё хорошо»): не публикуйте его.

Пустые `HC_*_URL` не ломают бэкап, но тогда о сбоях никто не сообщит.

## 4. Установка на сервер

Сначала должен быть выполнен `docs/deploy.md` (разделы 1–6): приложение работает в `/opt/aichat`.

1. Обновите код и запустите установку (первый проход создаст `/etc/aichat/restic.env` из шаблона):

   ```bash
   cd /opt/aichat
   git pull
   sudo ops/backup/install.sh
   ```

2. Заполните `restic.env`:

   ```bash
   sudo nano /etc/aichat/restic.env
   ```

   Значения — после `=`, **без кавычек и пробелов**:

   ```
   RESTIC_REPOSITORY=s3:s3.eu-central-1.amazonaws.com/<bucket>/aichat
   AWS_ACCESS_KEY_ID=<Access key сервера из раздела 2>
   AWS_SECRET_ACCESS_KEY=<Secret access key сервера из раздела 2>
   AWS_DEFAULT_REGION=eu-central-1
   HC_BACKUP_URL=<ping-url проверки aichat-backup>
   HC_RESTORE_URL=<ping-url проверки aichat-restore-test>
   ```

   Вставка в nano — правой кнопкой мыши (или Shift+Insert). Сохранить — Ctrl+O, Enter; выйти — Ctrl+X.

3. Второй проход: скачает образ restic, создаст пароль и репозиторий, включит таймеры:

   ```bash
   sudo ops/backup/install.sh
   ```

   В конце — таблица из двух таймеров (`aichat-backup.timer`, `aichat-restore-test.timer`) и «Готово».
   Повторный запуск безопасен: существующий пароль и репозиторий не трогаются.

4. **Сохраните пароль репозитория** в менеджер паролей — без него бэкапы бесполезны:

   ```bash
   sudo cat /etc/aichat/restic-password
   sudo cat /etc/aichat/restic.env
   ```

   В менеджер паролей — пароль (одна строка) и, отдельной записью, всё содержимое `restic.env`
   (понадобится при переезде на новый сервер).

5. Первый бэкап вручную (команда вернётся, когда бэкап закончится, обычно за минуту):

   ```bash
   sudo systemctl start aichat-backup.service
   sudo journalctl -u aichat-backup -n 50 --no-pager
   sudo cat /var/lib/aichat-backup/status.json
   ```

   В `status.json` должно быть `"result":"ok"`, а проверка `aichat-backup` на healthchecks.io —
   стать зелёной (**up**). Если `"result":"error"` — текст ошибки в поле `error` и в журнале.

6. Проверка восстановления вручную (чтобы не ждать 1-го числа):

   ```bash
   sudo systemctl start aichat-restore-test.service
   sudo cat /var/lib/aichat-backup/restore-test.json
   ```

   Должно быть `"result":"ok"`, проверка `aichat-restore-test` — зелёная.

Дальше всё идёт само. Полезное:

```bash
systemctl list-timers 'aichat-*'                       # когда следующие запуски
sudo cat /var/lib/aichat-backup/status.json            # итог последнего бэкапа
sudo journalctl -u aichat-backup -n 100 --no-pager     # журнал последних бэкапов
sudo journalctl -u aichat-restore-test -n 100 --no-pager
```

После `git pull` скрипты обновляются сами. Если в обновлении менялись файлы `ops/backup/systemd/`,
запустите `sudo ops/backup/install.sh` ещё раз.

## 5. Чек-лист проверки прав (на настоящем AWS, один раз после установки)

1. **Ключ сервера может делать бэкап:** шаг 5 раздела 4 дал `"result":"ok"`, и снапшот виден:

   ```bash
   cd /opt/aichat
   sudo ops/backup/restic.sh snapshots
   ```

2. **Ключ сервера не может удалить версию объекта и выключить версионирование.** На сервере, в первой
   строке замените `<bucket>`. Команды выполняют AWS CLI в Docker с ключом из `restic.env`: сначала ключ
   сервера создаёт пробный файл `permission-check.txt` (вне папки `aichat/`), затем пытается удалить
   именно эту версию и переключить версионирование:

   ```bash
   B=<bucket>
   AWS="sudo docker run --rm --env-file /etc/aichat/restic.env amazon/aws-cli:2.37.12"
   VID=$($AWS s3api put-object --bucket "$B" --key permission-check.txt --body /etc/hostname --query VersionId --output text)
   echo "$VID"
   $AWS s3api delete-object --bucket "$B" --key permission-check.txt --version-id "$VID"
   $AWS s3api put-bucket-versioning --bucket "$B" --versioning-configuration Status=Enabled
   ```

   - `echo` печатает id версии (длинную строку букв и цифр);
   - обе последние команды должны ответить
     `An error occurred (AccessDenied) when calling the DeleteObject operation…` и
     `…(AccessDenied) when calling the PutBucketVersioning operation…`.

   Любой другой ответ (нет ошибки, другая ошибка) — права настроены неверно: перепроверьте политику (раздел 2).
   Команды безопасны и при неверных правах: удаляется только пробная версия, а версионирование и так
   включено. Файл `permission-check.txt` можно потом удалить в консоли S3.

3. **У пользователя сервера только одна inline-политика** (на компьютере):

   ```powershell
   $env:AWS_PROFILE = "aichat-admin"
   aws iam list-user-policies --user-name aichat-backup-server
   aws iam list-attached-user-policies --user-name aichat-backup-server
   aws iam list-groups-for-user --user-name aichat-backup-server
   ```

   Ожидается: `"PolicyNames": ["aichat-backup"]`, `"AttachedPolicies": []`, `"Groups": []`.

## 6. Восстановление

### 6.0 Выбор снапшота

```bash
cd /opt/aichat
sudo ops/backup/restic.sh snapshots --tag aichat-db
```

Каждая строка — один снапшот: `ID` (8 символов) и `Time` (время **UTC**; Москва = UTC+3). Выберите
снапшот до порчи данных и подставляйте его ID вместо `<snapshot-id>`. Самый свежий можно указать словом `latest`.

### 6.1 На том же сервере (данные испорчены, сервер в порядке)

Не запускайте перед этим ручной бэкап: свежий снапшот с испорченными данными вытеснил бы сегодняшний
хороший при ротации.

Если вы настроили дополнительную локальную копию (`docs/deploy.md`, раздел 8, cron в 02:30), её
останавливать не нужно — если только восстановление не идёт около 02:30.

1. Сохраните текущую (испорченную) базу на всякий случай:

   ```bash
   cd /opt/aichat
   mkdir -p ~/backups && chmod 700 ~/backups
   F=~/backups/before-restore-$(date +%F-%H%M).dump
   docker compose exec -T db pg_dump -U aichat -Fc aichat > "$F" && test -s "$F" && echo "OK: копия создана: $F" || { rm -f "$F"; echo "ОШИБКА: копия не создана — не продолжайте"; }
   ls -lh ~/backups/before-restore-*.dump
   ```

   Должна быть строка `OK: копия создана: …`, а у файла в списке `ls` — размер больше нуля (обычно
   десятки КБ и больше). **Если напечатано `ОШИБКА` или любая другая ошибка — не продолжайте**: без этой
   копии отменить восстановление будет нельзя. Запомните полное имя файла, например
   `before-restore-2026-11-02-1530.dump`. В нём вся переписка — удалите его, когда убедитесь, что всё
   в порядке (шаг 3).

2. Только после `OK` — остановите ночной бэкап и приложение и восстановите снапшот:

   ```bash
   cd /opt/aichat
   sudo systemctl stop aichat-backup.timer
   docker compose stop app
   sudo -v
   sudo ops/backup/restic.sh dump <snapshot-id> /aichat.dump | docker compose exec -T db pg_restore -U aichat -d aichat --clean --if-exists --single-transaction
   ```

   - `sudo -v` заранее спрашивает пароль sudo, чтобы запрос не смешался с выводом восстановления.
   - `--single-transaction`: восстановление выполняется целиком или не выполняется вовсе — при ошибке
     база остаётся как была.

3. **Только если `pg_restore` не напечатал ошибок** — запустите приложение и ночной бэкап:

   ```bash
   docker compose start app
   docker compose ps
   sudo systemctl start aichat-backup.timer
   ```

   Через несколько дней, убедившись, что всё в порядке, удалите страховочную копию: `rm ~/backups/before-restore-*.dump`

4. Если `pg_restore` напечатал ошибки, приложение не запускайте. Варианты:
   - попробовать другой снапшот: команды `sudo -v` и `sudo ops/backup/restic.sh dump …` из шага 2;
   - вернуть базу, какой она была до восстановления, — команды ниже. Сначала посмотрите имя копии:

     ```bash
     ls -lh ~/backups/before-restore-*.dump
     ```

     Возьмите самый новый файл (дата и время — в имени) и в команде восстановления ниже замените
     `<полный путь к файлу копии>` целиком на путь к нему, например `~/backups/before-restore-2026-11-02-1530.dump`.

   Если вы прекращаете восстановление (и не возвращаете базу командами ниже) — верните хотя бы ночной
   бэкап: `sudo systemctl start aichat-backup.timer`.

   ```bash
   cd /opt/aichat
   docker compose stop app
   docker compose exec -T db pg_restore -U aichat -d aichat --clean --if-exists --single-transaction < <полный путь к файлу копии>
   docker compose start app
   sudo systemctl start aichat-backup.timer
   ```

### 6.2 На новом сервере

1. Выполните `docs/deploy.md`, разделы 1–5. В разделе 5 возьмите **сохранённый** `.env` из менеджера
   паролей, а не новый: тот же `FERNET_KEY` нужен для 2FA пользователей.
2. Запустите только базу, дождитесь её готовности и восстановите данные:

   ```bash
   cd /opt/aichat
   docker compose up -d db
   until docker compose exec -T db pg_isready -h 127.0.0.1 -U aichat -d aichat; do sleep 2; done
   sudo ops/backup/install.sh
   sudo nano /etc/aichat/restic.env
   ```

   В `restic.env` вставьте сохранённое содержимое (после атаки — с **новым** ключом сервера, раздел 7, шаг 6).
   Затем пароль репозитория из менеджера паролей (одна строка):

   ```bash
   sudo nano /etc/aichat/restic-password
   sudo chmod 600 /etc/aichat/restic-password
   sudo ops/backup/restic.sh snapshots --tag aichat-db
   sudo -v
   sudo ops/backup/restic.sh dump latest /aichat.dump | docker compose exec -T db pg_restore -U aichat -d aichat --clean --if-exists --single-transaction
   ```

   Если `pg_restore` напечатал ошибки — дальше не идите: база осталась пустой, повторите команду
   (или укажите другой снапшот вместо `latest`).

3. Запустите приложение и включите бэкапы (второй проход установки найдёт существующий репозиторий
   и пароль):

   ```bash
   docker compose up -d --build
   sudo ops/backup/install.sh
   ```

   Администратора заново создавать не нужно — он в восстановленной базе. Дальше — проверки из
   раздела 6 `docs/deploy.md` и шаги 5–6 раздела 4 здесь.

### 6.3 После взлома или удаления снапшотов

Сначала — раздел 7 (откат бакета), затем восстановление на новом сервере (6.2).

## 7. Откат бакета после атаки

Если сервер взломан или снапшоты пропали (restore-test пишет «нет снапшотов», `restic snapshots` пуст),
злоумышленник мог удалить объекты в бакете ключом сервера. Удалённое в версионированном бакете
хранится ещё **30 дней** — в это окно бакет можно вернуть на момент до атаки. Всё делается с вашего компьютера.

1. **Отзовите ключ сервера** — консоль AWS → **IAM** → **Users** → `aichat-backup-server` →
   **Security credentials** → в **Access keys** у ключа **Actions** → **Deactivate**. Или в PowerShell:

   ```powershell
   $env:AWS_PROFILE = "aichat-admin"
   aws iam list-access-keys --user-name aichat-backup-server
   aws iam update-access-key --user-name aichat-backup-server --status Inactive --access-key-id <AccessKeyId из предыдущей команды>
   ```

2. **Выберите момент до атаки** — время в UTC, не старше 30 дней, в формате `2026-11-02T10:00:00Z`
   (`Z` = UTC; Москва = UTC+3, т. е. 13:00 МСК → `10:00:00Z`). Подсказки: история пингов проверки
   `aichat-backup` на healthchecks.io (последний нормальный бэкап) и в консоли S3 у бакета переключатель
   **Show versions** (время удалений — у строк **Delete marker**).

3. **План отката** (ничего не меняет). В первой строке замените `<bucket>`, во второй — время из шага 2.
   Переменные `$bucket` и `$asOf` живут только в этом окне PowerShell: шаги 3–5 выполняйте в нём же
   (в новом окне сначала снова выполните первые три строки (`$bucket`, `$asOf`, `$env:AWS_PROFILE`) и `cd C:\Users\Vadim\Dropbox\AI_WebForm_Route`):

   ```powershell
   $bucket = "<bucket>"
   $asOf = "<время до атаки, например 2026-11-02T10:00:00Z>"
   $env:AWS_PROFILE = "aichat-admin"
   .venv\Scripts\python -m pip install boto3==1.43.118
   .venv\Scripts\python ops\backup\aws\undelete.py --bucket $bucket --as-of $asOf
   ```

   Печатается таблица `восстановить` / `удалить` / `без изменений` по ключам папки `aichat/` и итог.
   Время старше 30 дней скрипт не принимает (старые версии уже удалены lifecycle); `--force-old` снимает
   запрет, но восстановится не всё.

4. **Откат** — та же команда с `--apply`, затем снова без него:

   ```powershell
   .venv\Scripts\python ops\backup\aws\undelete.py --bucket $bucket --as-of $asOf --apply
   .venv\Scripts\python ops\backup\aws\undelete.py --bucket $bucket --as-of $asOf
   ```

   Во время `--apply` печатается каждый обработанный ключ. Повторный план должен показать
   `восстановить 0, удалить 0`. Если откат прервался ошибкой — просто запустите `--apply` ещё раз.

5. **Проверка репозитория** (спросит пароль репозитория — вставьте его из менеджера паролей):

   ```powershell
   docker run --rm -it -v "$env:USERPROFILE\.aws:/root/.aws:ro" -e AWS_PROFILE=aichat-admin -e AWS_DEFAULT_REGION=eu-central-1 "restic/restic:$(Get-Content ops\backup\RESTIC_VERSION)" -r "s3:s3.eu-central-1.amazonaws.com/$bucket/aichat" check
   ```

   Должно закончиться `no errors were found`. Список снапшотов — та же команда со словом `snapshots`
   вместо `check`. Если `check` жалуется на lock — та же команда с `unlock`, затем снова `check`.

6. **Новый сервер.** Взломанный сервер не используйте: переустановите систему (или создайте новый
   сервер) и восстановитесь по 6.2. Новый ключ сервера: **IAM** → **Users** → `aichat-backup-server` →
   **Security credentials** → **Create access key** (как в разделе 2); старый, отключённый ключ —
   **Actions** → **Delete**. Пароль репозитория остаётся прежним.

## 8. Ежемесячная проверка с вашего компьютера

Проверка не зависит от сервера: она видит, что на самом деле лежит в бакете (см. «Остаточный риск»
в разделе 1). Нужен профиль `aichat-admin` (раздел 2.1) и Docker Desktop. В первой строке замените
`<bucket>`; команда спросит пароль репозитория — вставьте его из менеджера паролей:

```powershell
$bucket = "<bucket>"
docker run --rm -it -v "$env:USERPROFILE\.aws:/root/.aws:ro" -e AWS_PROFILE=aichat-admin -e AWS_DEFAULT_REGION=eu-central-1 "restic/restic:$(Get-Content ops\backup\RESTIC_VERSION)" -r "s3:s3.eu-central-1.amazonaws.com/$bucket/aichat" snapshots
```

Что должно быть в списке (время — UTC):

- снапшоты за **каждый из последних 7 дней** (по одному в ночь);
- всего — **около 11 (обычно от 10 до 12)**: 7 ежедневных плюс 2–3 недельных и 1–2 месячных
  (один снапшот может одновременно считаться «дневным», «недельным» и «месячным», поэтому не 7+4+3);
  в первые месяцы после установки — меньше, это нормально.

Главный признак беды — **нет снапшота хотя бы за один из последних 7 дней** (или список вообще пуст),
а healthchecks.io при этом зелёный. Тогда считайте сервер взломанным: сразу раздел 7 (пока не прошли
30 дней). Одно лишь общее число 9–13 вместо 11 поводом для тревоги не является.

Поставьте себе ежемесячное напоминание в календаре, например на 2-е число (после проверки
восстановления 1-го числа).

По желанию — более длинное окно на обнаружение: в `ops/backup/aws/lifecycle.json` и в правиле
`expire-noncurrent-versions` бакета (раздел 2) увеличить `NoncurrentDays`, например до `90`. Удалённое
тогда хранится 90 дней (и всё это время оплачивается как хранение в S3). Это ваше решение; по умолчанию — 30.
Если увеличите — при откате (раздел 7) для момента старше 30 дней добавьте к `undelete.py` флаг `--force-old`.
