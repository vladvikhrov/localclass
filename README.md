# LocalClass

Децентрализованный P2P-мессенджер и файлообменник для учебного класса в локальной сети — без сервера и интернета.
Реализация ТЗ `LocalClass_TZ_v1.0.pdf` (архитектура v1.0): полный mesh, Ed25519-идентичность + TOFU,
UDP broadcast / mDNS / ручное подключение, length-prefix framing, персистентные `device_seq` / Lamport /
vector clock в SQLite, gossip без TTL + anti-entropy, tombstone, rate limiting, чанковая передача файлов с
манифестом и resume, сессии с кодом и QR, роли Student/Teacher, два потока логов, диагностика сети,
fault injection и harness, PySide6-GUI, сборка в Windows `.exe` с установщиком.

Платформа пользователей — **Windows**, разработка — **Linux**. Всё, что можно автоматизировать, автоматизировано:
тесты, кластер узлов на одной машине, сборка `.exe` и установщика (GitHub Actions или один PowerShell-скрипт).

---

## 1. Быстрый старт на Linux (разработка)

Системные пакеты (один раз; Qt 6.5+ требует `libxcb-cursor0`, `tmux` нужен для `make cluster`):

```bash
sudo apt-get install -y python3-venv libxcb-cursor0 tmux
```

```bash
make venv          # python3 -m venv .venv + зависимости (PySide6, cryptography, zeroconf, ifaddr, qrcode, pytest, pyinstaller)
make test          # все тесты, включая обязательные fault-injection сценарии ТЗ 15.3 (~1 мин)
make gui           # GUI одного узла (данные в run/GUI)
make cluster       # 3 headless-узла в tmux (A создаёт сессию, B и C подключаются по коду)
make cluster-gui   # 3 GUI-окна на одном ПК — каждый узел со своим device_id, портом и базой
```

Без make:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python -m localclass --device A --port 5001 --data ./run/A            # GUI
.venv/bin/python -m localclass.node --device B --port 5002 --data ./run/B       # узел без GUI, консоль команд (/help)
```

Флаги (одинаковы для GUI и headless): `--device МЕТКА` (dev-режим, loopback участвует в discovery), `--port`,
`--data КАТАЛОГ`, `--discovery-port`, `--name ИМЯ`, `--no-mdns`, `--no-discovery`, `--create-session НАЗВАНИЕ`,
`--join-code КОД`, `--join-session ID`, `--connect IP:PORT`, `--verbose`.
Без `--data` данные лежат в `%APPDATA%\LocalClass` (Windows) / `~/.local/share/LocalClass` (Linux).

## 2. Сборка Windows `.exe` и установщика

### Вариант A — автоматически через GitHub Actions (рекомендуется)

1. Создайте репозиторий на GitHub и запушьте проект:
   ```bash
   git init && git add . && git commit -m "LocalClass 1.0.0"
   git remote add origin git@github.com:<вы>/localclass.git && git push -u origin main
   ```
2. Workflow `.github/workflows/build-windows.yml` при каждом push в `main` прогоняет тесты на Linux, затем на
   `windows-latest` собирает `dist\LocalClass\` (PyInstaller), запускает smoke-тест exe, собирает установщик
   Inno Setup и публикует артефакт **LocalClass-windows** (вкладка Actions → запуск → Artifacts):
   `LocalClass-1.0.0-setup.exe` и `LocalClass-portable.zip`.
3. `make release` — ставит тег `v1.0.0` и пушит его: workflow дополнительно создаст GitHub Release с этими файлами.

### Вариант B — на любом Windows-ПК одной командой

Нужны Python 3.11+ (`winget install Python.Python.3.12`) и, для установщика, Inno Setup 6
(`winget install JRSoftware.InnoSetup`). Скопируйте папку проекта и выполните:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

Результат: `dist\LocalClass\LocalClass.exe` (портативная версия, папку можно просто скопировать) и
`dist\LocalClass-1.0.0-setup.exe` (установщик).

PyInstaller не умеет кросс-компилировать, поэтому сборка `.exe` на Linux невозможна — spec-файл
`LocalClass.spec` проверен сборкой Linux-бинарников (тот же spec, те же hidden imports).

### Что делает установщик

Ярлыки, опциональный автозапуск при входе, правило брандмауэра Windows для `LocalClass.exe`, TCP 45821 и
UDP 45820 (`netsh advfirewall`), удаление правил при деинсталляции.

**Предупредите класс заранее (ТЗ 17.3):** exe не подписан — SmartScreen покажет «Windows защитила ваш
компьютер» → «Подробнее» → «Выполнить в любом случае». При первом запуске Windows спросит про доступ в
частных сетях — разрешить.

## 3. Как пользоваться в классе

1. **Преподаватель**: вкладка «Сессия» → название, длительность → «Создать». На экране появляются код
   (например `K7F2-X9`) и QR.
2. **Ученики**: вводят код → «По коду» (сессии, видимые в сети, показаны списком — двойной клик) или вставляют
   текст QR («Копировать текст QR» у преподавателя). Подключение матчится по `session_id`, код — только для поиска.
3. Узлы находят друг друга автоматически (broadcast, резервно mDNS). Если «никого не видно» — вкладка
   **Диагностика**: чек-лист (интерфейс, broadcast, mDNS, TCP-порт), «обнаружено / доступно / недоступно» и
   список вероятных причин (изоляция клиентов, Firewall, разные подсети, VPN). Аварийный путь — «Пиры» →
   «Подключиться вручную» `IP:45821`.
4. **Чат**: каналы (преподаватель создаёт/закрывает), объявления, личные сообщения (двойной клик по участнику),
   лента ссылок, поиск, Markdown (`**жирный**`, `*курсив*`, `` `код` ``), удаление сообщений (свои — все,
   чужие — преподаватель), блокировка ученика в чате (правый клик).
5. **Файлы**: перетащите файл в окно или «Опубликовать файл». Перед публикацией большого файла — предупреждение
   с оценкой трафика. Участники скачивают двойным кликом; прогресс, скорость и оценка времени — в таблице
   передач. Обрыв связи, закрытие приложения, перезагрузка — передача продолжится с места обрыва (состояние
   в SQLite, докачиваются только недостающие/битые чанки). Владелец отдаёт не более 3 файлов одновременно,
   остальные ждут в очереди. Принятые файлы — `storage/files/`, никогда не запускаются автоматически.
6. **Логи**: журнал событий (для пользователя/преподавателя) и технический лог (для разработчика) с фильтрами
   по уровню, категории (сеть/файлы/чат/сессия/безопасность), тексту и узлу.
7. **Настройки**: имя, порты, список сетевых интерфейсов с галочками (отключите VirtualBox/Docker/VPN),
   автоприём личных файлов, лимиты — в `config/settings.json`.

## 4. Структура проекта (ТЗ 17.2)

```
localclass/
  config.py              пути (%APPDATA%/LocalClass), лимиты, settings.json
  bus.py                 Event Bus — единственный канал ядро ↔ UI
  logs.py                два потока логов: application log и event log
  core/identity.py       Ed25519, device_id = SHA-256(pubkey), подписи
  core/events.py         модель события, каталог типов
  core/store.py          SQLite: счётчики + события в одной транзакции, vector clock, tombstone, проекции, retention
  core/sync.py           vector clock, SYNC_REQUEST/RESPONSE с пагинацией, anti-entropy
  core/gossip.py         дедупликация по event_id, пересылка без TTL, rate limiting
  core/session.py        сессии, коды, QR
  core/node.py           Application Core: команды, права, сессии
  net/protocol.py        пакеты, версии, совместимость
  net/transport.py       length-prefix framing
  net/mesh.py            соединения, HELLO/AUTH + TOFU, heartbeat, reconnect, PEER_LIST
  net/discovery.py       UDP broadcast (SO_REUSEADDR/REUSEPORT), mDNS, ручной ввод
  net/interfaces.py      перечисление интерфейсов, подсказки VirtualBox/Docker/VPN
  net/diagnostics.py     экран диагностики с причинами
  files/manifest.py      FILE_OFFER: хеши чанков + полный SHA-256
  files/transfer.py      отдельное соединение, окно ACK, backpressure, resume, очередь
  files/storage.py       санитизация пути, карантин incoming/, atomic rename
  ui/app/                PySide6 GUI (bridge: ядро в отдельном потоке asyncio)
  ui/debug/              отладочный интерфейс = консольный REPL localclass/node.py
  testing/faults.py      packet loss, latency, partition
  testing/harness.py     N узлов в одном процессе
  node.py                точка входа без GUI
tests/                   протокол, хранилище, 8 обязательных сценариев ТЗ 15.3, файлы
docs/PROTOCOL.md         спецификация протокола (раздел 16 ТЗ)
LocalClass.spec, installer/LocalClass.iss, scripts/, .github/workflows/  — сборка и автоматизация
```

## 5. Тесты (ТЗ 15.3) — `make test`

| Сценарий | Тест |
|---|---|
| Узел офлайн, затем возврат — восстановление без дубликатов (< 10 с) | `test_offline_then_return` |
| 20 % потери пакетов при живом соединении — anti-entropy | `test_packet_loss_anti_entropy` |
| A ✗ C при живых A—B—C — доставка через B | `test_partition_relay_through_middle` |
| Перезапуск процесса в середине передачи — resume из SQLite | `test_resume_after_restart` |
| Перезапуск узла с базой — персистентность lamport/device_seq | `test_restart_keeps_counters` |
| DELETED раньше CREATED — tombstone | `test_deleted_before_created_*` |
| v1 ↔ v2 — совместимость, игнорирование неизвестного | `test_v1_v2_compat` |
| Пир превышает лимит — rate limiting и восстановление | `test_rate_limit_and_recovery` |
| Дополнительно: чат не блокируется во время передачи, честный отказ без прямого пути, очередь исходящих, TOFU | `test_files.py`, `test_tofu_conflict` |

Fault injection доступен и в консоли узла: `/fault <device_id_A> <device_id_B> loss 0.2` (`latency 0.3`, `partition on`).

## 6. Принятые решения и отклонения от ТЗ

* **`device_seq` ведётся на пару (device_id, session_id)**, а не глобально на устройство: vector clock скоупится
  сессией (ТЗ 11.3), и посессионный счётчик нужен, чтобы в векторе не было «дыр» от событий других сессий.
  Lamport — один на устройство.
* **SQLite через стандартный `sqlite3` вместо SQLAlchemy**: критичны явные транзакции (`BEGIN IMMEDIATE …
  COMMIT` для счётчика и события), ORM здесь только скрывал бы их.
* **Передача файлов — pull-модель**: получатель всегда инициирует файловое соединение и сообщает, что у него
  уже есть. Это делает resume после обрыва, закрытия и перезагрузки одним и тем же путём кода.
* **PEER_CONNECTED / PEER_DISCONNECTED** — локальные записи журнала событий, не реплицируются по gossip
  (иначе каждый узел рассылал бы классу свои соединения).
* Роли — cooperative security (ТЗ 4.3): проверяются клиентом. Подписанные команды преподавателя — после v1.0.
* TLS, relay файлов, swarm — вне v1.0, как в ТЗ.

## 7. Типичные проблемы

| Симптом | Что делать |
|---|---|
| «Could not load the Qt platform plugin "xcb"» на Linux | нет системной библиотеки Qt: `sudo apt-get install -y libxcb-cursor0 tmux` |
| «Не удалось запустить узел: порт занят» | другой экземпляр LocalClass, либо `--port 45822` |
| Никого не видно | Диагностика → причины; Firewall на UDP 45820/TCP 45821; ручное подключение по IP |
| Обнаружен, но недоступен | TCP режется (изоляция клиентов на точке доступа / Firewall на удалённом ПК) |
| SECURITY WARNING | у узла изменился ключ (переустановка / подмена) — решите на вкладке «Пиры» |
| «Версия LocalClass устарела» | несовместимый protocol_version — обновите приложение |
