# ShelterCheck

ShelterCheck автоматично контролює перевірки в Signal:

1. бачить повідомлення «Всі в укритті?»;
2. фіксує snapshot звільнених і перевіряє reactions `➕` саме для нього;
3. якщо проміжний звіт увімкнено, через заданий час, наприклад 5 хвилин, надсилає його;
4. у final deadline, наприклад через 10 хвилин, надсилає authoritative звіт;
5. завершує перевірку одразу після `N/N`, не чекаючи наступного deadline;
6. старі активні перевірки silent-expire за TTL, типово через 6 годин.

Програма також дозволяє авторизованим користувачам команд керувати списком
звільнених через Signal.

Поточна версія — **v0.2.3**. У ній `/check <текст>` може вибрати повідомлення
будь-якого автора, автоматична reaction host-акаунта стала configurable, а
готовність signal-cli означає не лише відкритий HTTP port, а й рівно один
завантажений account.

## Швидкий старт

Цей розділ розрахований на користувача без досвіду роботи з Python і systemd.
Команди можна копіювати в terminal по черзі.

### Що потрібно

- Linux із systemd;
- постійний доступ до Internet;
- Python 3.12 або новіший;
- Git;
- актуальний `signal-cli`, встановлений для всієї системи;
- Signal-акаунт на телефоні.

Docker для цього deployment не використовується і зараз не підтримується.

Для актуальних Ubuntu/Debian потрібні базові пакети:

```bash
sudo apt update
sudo apt install git python3 python3-venv curl
python3 --version
```

Для Arch Linux:

```bash
sudo pacman -Syu --needed git python curl
python3 --version
```

Якщо версія Python нижча за 3.12, спочатку встановіть підтримувану версію
Python для вашого дистрибутива. Installer не підміняє системний Python і не
підключає сторонні package repositories.

### Встановити signal-cli

Спочатку перевірте, чи він уже є:

```bash
signal-cli --version
signal-cli daemon --help
```

ShelterCheck перевірений із `signal-cli 0.14.7`. `signal-cli` потрібно регулярно
оновлювати: старі версії можуть перестати працювати після змін Signal Server.

Для x86_64 використовуйте поточний офіційний Linux release або пакет вашого
дистрибутива. Актуальні вимоги й команди публікує
[upstream signal-cli](https://github.com/AsamK/signal-cli#installation). JVM build
актуальної гілки потребує Java 25; офіційний native Linux archive не потребує
окремого Java runtime.

Installer ShelterCheck сам `signal-cli` не завантажує. Якщо `signal-cli` відсутній,
він зупиниться з поясненням. Це захищає від встановлення неправильного binary для
вашої архітектури.

### Встановити ShelterCheck

```bash
git clone <repository>
cd signal-shelter-check
sudo ./deploy/install.sh
```

Скрипт можна запускати повторно. Він оновлює програму й unit-файли, але не
перезаписує production config, roster, SQLite або Signal account state.

### Один раз після встановлення

#### 1. Прив’язати Signal

Не запускайте services до завершення linking. Виконайте:

```bash
sudo -u sheltercheck /opt/sheltercheck/signal-cli \
  --data-dir /var/lib/sheltercheck/signal-cli \
  link -n "ShelterCheck server"
```

На телефоні відкрийте:

```text
Signal на телефоні
        ↓
Налаштування / Settings
        ↓
Linked devices / Пов’язані пристрої
        ↓
Link new device / Додати пристрій
        ↓
відскануйте QR-код із terminal
```

Команда `link` є актуальним upstream способом linking і показує URI/QR для нового
пристрою. Якщо ваша версія показує лише `sgnl://linkdevice?...`, перевірте:

```bash
signal-cli link --help
```

та перетворіть URI на QR-код, наприклад за допомогою `qrencode`, як описано в
[офіційному manual](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli.1.adoc#link).
URI діє недовго — не публікуйте його й не зберігайте в Git.

Після сканування перевірте:

```bash
sudo -u sheltercheck /opt/sheltercheck/signal-cli \
  --data-dir /var/lib/sheltercheck/signal-cli \
  listAccounts
```

У цьому data directory має бути рівно один linked account. Копіювання Signal state
зі стороннього ПК не є рекомендованим способом першого deployment.

#### 2. Заповнити конфіг

```bash
sudo nano /etc/sheltercheck/config.toml
```

Замініть `REPLACE_WITH_...` на справжні group IDs. Заповніть дозволені ACI UUID.
Особливо важливі поля:

```toml
monitor_group_id = "SIGNAL_MONITOR_GROUP_ID"
report_group_id = "SIGNAL_REPORT_GROUP_ID"
command_group_id = "SIGNAL_COMMAND_GROUP_ID"

trigger_author_uuids = [
    "ACI_UUID_АВТОРА_ПЕРЕВІРКИ"
]

command_author_uuids = [
    "ACI_UUID_АВТОРИЗОВАНОГО_КОРИСТУВАЧА_КОМАНД"
]

auto_host_reaction = true
```

Порожній `command_author_uuids` безпечно вимикає адміністративні Signal-команди.
Порожній `trigger_author_uuids` має іншу поведінку: будь-який учасник monitor group
може створити перевірку.

`auto_host_reaction` — optional boolean із default `true`. При `true` host-акаунт
робить одну at-most-once attempt поставити одну з `accepted_reactions` на новий
authorized `trigger_text`. При `false` ця outgoing reaction не надсилається, але
AlarmSession створюється, timers працюють і reactions інших користувачів
відстежуються як завжди. Старий config без цього key зберігає enabled-поведінку.

Production paths уже задані правильно:

```toml
roster_file = "/var/lib/sheltercheck/roster.csv"
released_file = "/var/lib/sheltercheck/released_today.txt"
state_db = "/var/lib/sheltercheck/state.sqlite3"
```

Lifecycle timers:

```toml
intermediate_check_enabled = true
intermediate_check_seconds = 300
wait_seconds = 600
active_check_ttl_seconds = 21600
```

`intermediate_check_enabled` — optional boolean із default `true`. Старий deployed
config без цього key зберігає поточну поведінку.

Коли проміжний звіт увімкнений:

```toml
intermediate_check_enabled = true
intermediate_check_seconds = 300
wait_seconds = 600
```

```text
5 хв  → проміжний звіт
10 хв → основна перевірка
```

У цьому режимі має виконуватися
`0 < intermediate_check_seconds < wait_seconds < active_check_ttl_seconds`.
Проміжний report надсилається максимум один раз, не завершує session і показує
current missing snapshot цієї перевірки.

Щоб повністю пропустити проміжний звіт:

```toml
intermediate_check_enabled = false
wait_seconds = 600
```

```text
10 хв → основна перевірка
```

При `false` проміжне повідомлення та send attempt не створюються.
`intermediate_check_seconds` усе ще має бути positive integer, але не впливає на
runtime behavior. Early completion після `N/N`, final evaluation за `wait_seconds`
і silent expiry за `active_check_ttl_seconds` працюють незалежно від цього параметра.
Без explicit timing keys використовуються defaults `300` для intermediate deadline
і `21600` для TTL.

Final report окремий і authoritative. Completion також має окремий короткий формат,
наприклад:

```text
✅ Усі 17/17 відмітилися.
Перевірку завершено за 4 хв 21 с.
```

Якщо final report уже успішно надісланий, completion робить одну edit attempt цього
report. Якщо final report ще не існує, completion робить одну окрему send attempt.

#### 3. Заповнити roster

```bash
sudo nano /var/lib/sheltercheck/roster.csv
```

Формат:

```csv
signal_aci,display_name,phone
ACI_UUID,Прізвище І.П.,+380XXXXXXXXX
```

Кожні `signal_aci` і `display_name` мають бути унікальними. Телефон записується у
міжнародному E.164 форматі з `+`.

#### 4. Заповнити список звільнених на сьогодні

```bash
sudo nano /var/lib/sheltercheck/released_today.txt
```

По одному точному `display_name` на рядок:

```text
Тестовий А.А.
Приклад Б.Б.
Умовний В.В.
```

#### 5. Перевірити файли

```bash
sudo -u sheltercheck /opt/sheltercheck/.venv/bin/python \
  -m sheltercheck \
  --config /etc/sheltercheck/config.toml \
  --validate-config
```

#### 6. Увімкнути автоматичний запуск

```bash
sudo systemctl enable --now signal-cli.service
sudo systemctl enable --now sheltercheck.service
```

Після reboot обидва services запустяться автоматично.

### Перевірити, що все працює

```bash
systemctl status signal-cli.service --no-pager
systemctl status sheltercheck.service --no-pager
```

Повна автоматична перевірка:

```bash
sudo -u sheltercheck /opt/sheltercheck/.venv/bin/python \
  -m sheltercheck \
  --config /etc/sheltercheck/config.toml \
  --health
```

У кінці має бути:

```text
STATUS: OK
```

Healthcheck не надсилає повідомлень у Signal.
`Signal daemon: OK` означає, що `GET /api/v1/check` відповів і `listAccounts`
повернув рівно один account. Відкритого порту без завантаженого account недостатньо.

### Щовечора: оновити released_today

Найпростіше відкрити файл:

```bash
sudo nano /var/lib/sheltercheck/released_today.txt
```

Перезапуск не потрібен. Новий список використає наступна перевірка; уже активна
перевірка збереже старий snapshot.

Перевірити список:

```bash
sudo -u sheltercheck /opt/sheltercheck/.venv/bin/python \
  -m sheltercheck --config /etc/sheltercheck/config.toml \
  released validate
```

Показати список:

```bash
sudo -u sheltercheck /opt/sheltercheck/.venv/bin/python \
  -m sheltercheck --config /etc/sheltercheck/config.toml \
  released show
```

Безпечно імпортувати підготовлений файл:

```bash
sudo -u sheltercheck /opt/sheltercheck/.venv/bin/python \
  -m sheltercheck --config /etc/sheltercheck/config.toml \
  released import /path/to/today.txt
```

Import перевіряє точний збіг усіх імен із roster. Якщо знайдено невідоме ім’я або
дублікат, production-файл не змінюється. Після успішної перевірки файл замінюється
атомарно.

Released list також можна змінювати дозволеними Signal-командами `/setrt`, `/getrt`,
`/addrt`, `/delrt`, `/clearrt confirm`. Повна довідка доступна через `/help`.

| Signal-команда | Дія |
|---|---|
| `/setrt` + імена з наступних рядків | Повністю замінити список після перевірки всіх імен. |
| `/getrt` | Показати поточний список. |
| `/addrt` + імена | Додати людей, не дублюючи тих, хто вже є. |
| `/delrt` + імена | Видалити людей зі списку. |
| `/clearrt confirm` | Очистити список; без слова `confirm` очищення не відбудеться. |
| `/check` | Виконати manual evaluation останньої стандартної перевірки. |
| `/check <текст>` | Почати або перевірити session для найновішого отриманого повідомлення будь-якого автора з exact normalized text. |
| `/status` | Показати стан системи, uptime і detail найновішої активної перевірки. |
| `/help` | Показати коротку довідку. |

`/check` не створює новий AlarmSession і не скидає trigger time, intermediate,
final або TTL останньої standard-перевірки. Якщо вона terminal, новий report не
створюється.

`/check <текст>` потрібна для нестандартного контрольного повідомлення, якого немає
в `trigger_texts`. ShelterChecker шукає найновіше повідомлення лише в
`monitor_group_id`, незалежно від автора, і лише за exact equality після
`normalize_text()` (Unicode NFKC, trim/collapse whitespace, casefold). Fuzzy,
substring і regex matching немає. Це виключення стосується тільки candidate message:
саму `/check` і далі може виконати лише ACI з `command_author_uuids` у
`command_group_id`. Автоматичні `trigger_texts` і далі застосовують
`trigger_author_uuids`.

ShelterChecker не читає server-side Signal history. Він може знайти тільки message і
reactions, events яких реально отримав під час роботи. Для цього локально зберігається
обмежена 24-годинна candidate history тільки для monitor group, але для всіх його
авторів. Add/remove reactions, отримані до `/check <текст>`, replay-яться за event
timestamp, тому manual session одразу має правильний current response state.

Для custom session original timestamp релевантного повідомлення залишається reaction target і часом
«Контрольного повідомлення». `tracking_started_at_ms` фіксується окремо в момент
`/check <текст>`; intermediate/final/TTL та elapsed рахуються від нього. Snapshot
released list також фіксується саме в момент запуску manual check, а не в момент
надсилання релевантного повідомлення.

### Логи, restart і stop

Подивитися live-логи:

```bash
journalctl -u sheltercheck.service -f
journalctl -u signal-cli.service -f
```

Перезапустити програму:

```bash
sudo systemctl restart sheltercheck.service
```

Зупинити систему:

```bash
sudo systemctl stop sheltercheck.service
sudo systemctl stop signal-cli.service
```

### Оновлення

У директорії clone:

```bash
git pull
sudo ./deploy/update.sh
```

Update повторно використовує безпечний installer, перевіряє config, оновлює
readiness helper і unit-файли. Якщо services працювали, він спочатку перезапускає
signal-cli, чекає semantic readiness, а тоді перезапускає ShelterCheck. Config,
roster, SQLite та Signal state не видаляються й не перезаписуються.

### Backup

```bash
sudo ./deploy/backup.sh
```

Архів створюється у `/var/backups/sheltercheck/` із правами `600` і містить:

- `/etc/sheltercheck/config.toml`;
- `/var/lib/sheltercheck/roster.csv`;
- `/var/lib/sheltercheck/released_today.txt`, якщо файл існує;
- консистентну SQLite-копію `/var/lib/sheltercheck/state.sqlite3`.

Інший каталог можна вказати аргументом:

```bash
sudo ./deploy/backup.sh /mnt/protected-backups
```

Backup ніколи не можна зберігати в Git repository. Signal account state навмисно
не входить у звичайний archive: він дає доступ до linked Signal device й потребує
окремого зашифрованого backup зі строгим контролем доступу.

### VM

Рекомендована мінімальна VM:

```text
Debian/Ubuntu Server
1 vCPU
1 GB RAM
10 GB disk
постійний network access
```

GUI не потрібен. VM повинна залишатися увімкненою, Internet має працювати постійно.
systemd автоматично запускає services після reboot.

### Raspberry Pi / Orange Pi / ARM64

- використовуйте 64-bit OS: Debian, Armbian або Raspberry Pi OS;
- потрібен Python 3.12+;
- актуальний JVM build `signal-cli` потребує відповідного Java runtime;
- переконайтеся, що конкретний `signal-cli` build і native `libsignal-client`
  підтримують ARM64;
- до install запустіть `signal-cli --version` і `signal-cli daemon --help`;
- Ethernet надійніший за Wi-Fi для постійного service.

Installer розпізнає `aarch64/arm64`, але не завантажує binary автоматично. Upstream
попереджає, що готові native libraries насамперед постачаються для x86_64 Linux;
ARM64 build слід отримати з надійного джерела для вашої OS або зібрати за upstream
інструкцією. SBC-specific hacks у ShelterCheck відсутні.

### Troubleshooting

| Проблема | Що робити |
|---|---|
| `Signal daemon: connection refused` | Перегляньте `systemctl status signal-cli` та `journalctl -u signal-cli -n 100`; service сам retry-ить startup кожні 10 секунд. |
| `no accounts loaded` після boot | Readiness навмисно валить цей startup attempt і systemd перезапускає signal-cli. Перевірте наступні записи journal; ручний restart зазвичай не потрібен. |
| `unknown display_name` | У `released_today.txt` має бути точний `display_name` із `roster.csv`. Перевірте крапки, ініціали та пробіли всередині ПІБ. |
| `trigger_author_uuids is empty` | Зараз будь-який учасник monitor group може запустити перевірку. Додайте ACI UUID дозволених авторів у `/etc/sheltercheck/config.toml`. |
| `Roster: 0 members` | Перевірте `/var/lib/sheltercheck/roster.csv`: після CSV header має бути хоча б один коректний рядок. |
| Програма не реагує на «Всі в укритті?» | Перевірте `signal-cli`, monitor group ID, ACI автора, точний trigger text і `journalctl -u sheltercheck -n 100`. |
| Після reboot не працює | Перевірте `systemctl is-enabled ...`, `listAccounts` у service data-dir і обидва journals. Якщо unit disabled — повторіть `sudo systemctl enable --now ...`. |
| `--validate-config` показує permission denied | Перевірте власника й права через `sudo ./deploy/install.sh`; installer відновлює безпечні production permissions без зміни вмісту. |
| Service постійно restart | Виконайте `journalctl -u signal-cli -u sheltercheck -n 100 --no-pager` і виправте першу readiness/config помилку. Між attempts є пауза 10 секунд. |

Короткий checklist, якщо немає реакції на trigger:

1. `systemctl status signal-cli` показує `active (running)`, а не `activating`/`failed`;
2. `--health` показує `Signal daemon: OK` — тобто HTTP і `listAccounts` готові;
3. `monitor_group_id` збігається з реальною Signal-групою;
4. ACI автора є в `trigger_author_uuids`, якщо allowlist не порожній;
5. текст є в `trigger_texts`;
6. причина відсутня у `journalctl -u sheltercheck -n 100`.

## Важливі правила безпеки

### Тільки один активний instance

Для одного workflow підтримується:

```text
ONE active ShelterCheck instance
```

Не запускайте одночасно production-копію на laptop і Raspberry Pi. Обидві побачать
той самий trigger і можуть створити два report messages. Distributed leader election
у поточній версії навмисно не реалізований.

### Signal account state

Linked `signal-cli` device має доступ до стану Signal account. Не розміщуйте
особистий linked account на VM, яку контролює стороння людина.

Для постійного deployment використовуйте:

- окрему контрольовану Linux-машину; або
- окремий Signal account для сервісу, якщо це організаційно допустимо.

Ніколи не копіюйте `/var/lib/sheltercheck/signal-cli/` у GitHub, звичайне cloud
сховище або незашифрований backup.

## Для технічних користувачів

### Архітектура

```text
signal-cli HTTP/SSE on 127.0.0.1:8080
        ↓
raw upstream event
        ↓
event_parser → normalized MessageEvent / ReactionEvent
        ↓
slash command authorization → released-list service → group reply
        або
AlertTracker → SQLite state → report send/edit
```

ShelterCheck використовує лише upstream endpoints:

- `GET /api/v1/check` — доступність HTTP daemon;
- `GET /api/v1/events` — SSE events;
- `POST /api/v1/rpc` — `listAccounts`, Signal JSON-RPC send/edit/reaction.

Ці endpoints документовані в
[signal-cli JSON-RPC manual](https://github.com/AsamK/signal-cli/blob/master/man/signal-cli-jsonrpc.5.adoc).
Config приймає лише loopback daemon URL. systemd unit явно bind-ить daemon до
`127.0.0.1:8080`; LAN bind не використовується.

### Production filesystem layout

```text
/opt/sheltercheck/
├── .venv/
├── signal-cli -> system-wide signal-cli executable
└── signal_cli_readiness.py       root:root 0755

/etc/sheltercheck/
└── config.toml                 root:sheltercheck 0640

/var/lib/sheltercheck/
├── roster.csv                  sheltercheck:sheltercheck 0600
├── released_today.txt          sheltercheck:sheltercheck 0600
├── state.sqlite3               sheltercheck:sheltercheck 0600
└── signal-cli/                 sheltercheck:sheltercheck 0700
```

Production state не зберігається у clone. Relative paths у config, як і раніше,
резолвляться від директорії самого config, а не current working directory. Production
template використовує absolute paths, тому service однаково працює з будь-якого CWD.

### systemd

`signal-cli.service` запускається від `sheltercheck`, використовує окремий data-dir,
не друкує повні received messages у journal і слухає лише loopback. Його
`ExecStartPost` чекає HTTP API та виконує side-effect-free `listAccounts`. Порожній
або неоднозначний account set завершує startup attempt помилкою; systemd зупиняє
дефектний daemon і запускає новий attempt через 10 секунд. Start-rate limit
вимкнений, тому тимчасово недоступні DNS/Internet не залишають service назавжди
failed; retry loop не tight завдяки `RestartSec=10`.
Readiness logs показують лише кількість account-ів і не друкують номер телефону.

`sheltercheck.service`:

- працює від `sheltercheck`, не root;
- має `Wants/After=signal-cli.service`;
- виконує `--validate-config` і той самий semantic readiness probe перед стартом;
- пише logs у systemd journal;
- використовує `Restart=on-failure`, паузу 10 секунд і безпечний нескінченний retry;
- має `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`, `ProtectHome`,
  `ProtectSystem=strict`;
- має write access лише до `/var/lib/sheltercheck`.

`Wants`, а не hard `Requires`, обрано навмисно: після першого failed startup
signal-cli hard dependency могла б залишити ShelterCheck у стані `dependency failed`,
не запустивши його після пізнішого успішного auto-restart. Власний `ExecStartPre`
все одно не пропускає application process, доки account не завантажений.

Startup flow:

```text
network-online.target
        ↓
signal-cli daemon + loopback HTTP
        ↓
ExecStartPost: GET /check + listAccounts == exactly one
        ├─ not ready → startup failed → 10 s → restart signal-cli
        └─ ready
              ↓
sheltercheck ExecStartPre: config + повторна semantic readiness
              ↓
ShelterCheck event loop / SSE
```

### Identity, alert і persistence semantics

Member identity — лише ACI UUID із roster. Profile names і phone fields із Signal
events не використовуються як identity keys.

Alert identity:

```text
(monitor group ID, trigger author ACI, trigger Signal timestamp)
```

Reaction routing перевіряє group, точний target timestamp і target author, якщо він
присутній. На момент trigger released members snapshot-яться у SQLite. Пізні зміни
`released_today.txt` не змінюють активний alert.

AlarmSession має чітку state machine:

```text
active:   pending → reported
terminal: completed | expired | stale | error
```

`completed` і `expired` справді terminal: наступні reaction add/remove не змінюють
responded/missing state, не reopen session і не породжують send/edit. `pending`
переходить у `reported` після final evaluation із missing members. Останній accepted
`+` у будь-якому active state одразу запускає completion. TTL має пріоритет над усіма
reports та silent-переводить `pending/reported` у `expired`.

`trigger_timestamp_ms` — timestamp original Signal message для reaction routing і
display time. `tracking_started_at_ms` — початок timer lifecycle. Для automatic
standard alarm вони однакові; для custom `/check <текст>` tracking починається в
момент команди.

Кожна logical outgoing operation записується у SQLite до Signal RPC зі станом, що
розрізняє `not_due`, `due_not_attempted`, success, explicit failure, uncertain
delivery і skipped. Гарантія writes — **at-most-once attempt**. ShelterChecker не
retry-ить failed report sends, edits, host reactions або command replies у ticker,
після іншої події чи після restart. Timeout/connection reset після початку RPC
вважається uncertain і також ніколи автоматично не повторюється. Це свідомо віддає
перевагу пропущеному повідомленню над duplicate Signal message/reaction.

Signal send results перевіряються не лише за `timestamp`, а й за recipient `results`.
Перший `RATE_LIMIT_FAILURE` або proof-required challenge latch-ить centralized
outgoing circuit breaker у `SignalClient`. Усі наступні report sends, edits, host
reactions і command replies блокуються до RPC. Автоматичного reset за
`retryAfterSeconds` немає; outgoing залишається disabled до restart process. Процес
не завершується: incoming SSE, reaction tracking, SQLite transitions, TTL і local
cleanup продовжують працювати.
Rate limit записується лише в local critical log — ShelterChecker не намагається
повідомити про нього через Signal.

SQLite schema migrations виконуються backward-safe. Невідома або пошкоджена schema
має завершити startup/health із помилкою, а не мовчки видалити state.

`/status` показує semantic health Signal, roster/current released counts, кількість лише
`pending/reported` sessions, last check і uptime поточного process із monotonic clock.
`Signal: 🟢 connected` можливий тільки після успішного HTTP check і рівно одного
account у `listAccounts`; HTTP daemon із порожнім account set показується як
`🔴 disconnected`.
Нижній block показує тільки latest active session та використовує її immutable
released snapshot:

```text
Перевірка: активна
Контрольне повідомлення: 23:41
Минуло: 6 хв 12 с
Відмітилися: 14/17
Очікуються: 3
```

Верхній `Released today` при цьому читає current released list. Якщо active session
немає, block містить `Перевірка: неактивна`.

### Signal-команди

Команда виконується лише якщо одночасно:

```text
message.group_id == command_group_id
message.sender_aci in command_author_uuids
message.text starts with "/"
```

Allowlist порожній — команди вимкнені. Неавторизовані slash messages ігноруються без
відповіді й не передаються у trigger logic. Payload із ПІБ не пишеться в INFO logs.

Імена для `/setrt` і `/addrt` порівнюються точно з roster після trim країв. Atomic
write використовує private temporary file в тому самому filesystem, `flush`, `fsync`
і `os.replace`. Process-local `asyncio.Lock` серіалізує зміни.

### Privacy model

- `signal-cli` API не можна публікувати через reverse proxy або firewall port-forward;
- normal logs не містять телефонів або повних command payload;
- observed history містить лише 24 години candidate messages усіх авторів у monitor
  group та потрібні reaction events, щоб `/check <текст>` не залежав від автора; це
  не server-side архів Signal chat;
- unit використовує `--scrub-log --no-receive-stdout`;
- `tools/dump_events.py` може показати personal data й призначений лише для локальної
  діагностики в ignored `debug_events/`;
- real config, data, SQLite, Signal state, backups і virtualenv ігноруються Git.

Synthetic fixtures у `tests/fixtures/` не є raw events реальних користувачів.

### Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
cp config.example.toml config.toml
mkdir -p data state
cp examples/roster.example.csv data/roster.csv
cp examples/released_today.example.txt data/released_today.txt
```

Для local development змініть у ignored `config.toml`:

```toml
state_db = "state/state.sqlite3"
roster_file = "data/roster.csv"
released_file = "data/released_today.txt"
```

Relative paths будуть розв’язані від директорії `config.toml`, тому запуск із іншого
CWD також підтримується, якщо передати absolute `--config` path.

Команди розробника:

```bash
python -m sheltercheck --validate-config
python -m sheltercheck --dry-run
python -m sheltercheck --health
pytest -q
bash -n deploy/install.sh
bash -n deploy/update.sh
bash -n deploy/backup.sh
```

Dry-run читає реальні events, але не надсилає Signal messages, використовує in-memory
SQLite та in-memory released-list copy. Production files не змінюються.

GitHub Actions тестує Python 3.12 і 3.13, compile/import, shell syntax і весь pytest
suite без реального Signal account або network integration.

### Raw-event compatibility

Parser підтримує лише upstream structures, підтверджені synthetic fixtures:

- incoming: `envelope.dataMessage`;
- linked-device outgoing: `envelope.syncMessage.sentMessage`;
- group: `groupInfo.groupId`;
- reaction: `emoji`, `targetAuthorUuid`, `targetSentTimestamp`, `isRemove`.

Перед deployment з новою major/minor версією `signal-cli` за потреби перевірте events
через `tools/dump_events.py`, відредагуйте personal data і тільки тоді додавайте нову
synthetic fixture. Не розширюйте parser на основі припущень.
