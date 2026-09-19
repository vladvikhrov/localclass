# LocalClass — спецификация протокола v1

Документ закрывает вопросы раздела 16 ТЗ. Реализация: `localclass/net/protocol.py`, `transport.py`, `mesh.py`,
`core/sync.py`, `core/gossip.py`, `files/transfer.py`.

## 1. Транспорт и фрейминг

* TCP, один порт на узел (по умолчанию **45821**). Он же принимает и управляющие, и файловые соединения:
  тип соединения определяется первым пакетом (`HELLO` → управляющее, `FILE_RESUME` → файловое).
* Фрейм: `uint32 big-endian length` + `payload`. Строковые разделители не используются.
* Управляющее соединение: payload = JSON (UTF-8). Максимум **1 МиБ** на пакет (`limits.max_packet_size`).
* Файловое соединение: первый байт payload — тип фрейма: `0x00` JSON-пакет, `0x01` бинарный чанк
  (`uint32 chunk_index` + данные). Максимум `max_chunk_size + 16`.
* JSON: без `NaN`/`Infinity`, целые ≤ 2⁵³. Нарушение — пакет отбрасывается.

## 2. Пакет

```json
{"protocol_version": 1, "message_type": "EVENT", "request_id": "hex", "sender_id": "device_id", "payload": {}}
```

Совместимость: узел принимает `protocol_version` от 1 до 2 (`MIN_COMPATIBLE..MAX_COMPATIBLE`).
Неизвестный `message_type` и неизвестные поля игнорируются. Версия вне диапазона →
ответ `PROTOCOL_VERSION_MISMATCH {protocol_version}` и закрытие соединения; в UI — «Версия LocalClass устарела».

## 3. Рукопожатие (HELLO / HELLO_ACK / AUTH)

| Шаг | Отправитель | Пакет | payload |
|---|---|---|---|
| 1 | инициатор | `HELLO` | `device_id, public_key(b64), session_id, port, addresses[], nonce(b64,16 байт), display_name, app_version` |
| 2 | акцептор | `HELLO_ACK` | то же + `signature = Ed25519(nonce инициатора)` |
| 3 | инициатор | `AUTH` | `signature = Ed25519(nonce акцептора)` |

Проверки на обеих сторонах: `device_id == SHA-256(public_key)`, подпись верна, `session_id` совпадает
(иначе `ERROR {reason: "SESSION_MISMATCH"}`), TOFU — отпечаток совпадает с сохранённым
(иначе `SECURITY WARNING`, соединение отклоняется до решения пользователя). Отказ — `ERROR {reason}`.

**Дубль соединений** (оба узла соединились одновременно): выживает соединение, инициированное меньшим
`device_id`; второе закрывается обеими сторонами по одному правилу.

После установления обе стороны шлют `PEER_LIST {peers: [{device_id, addresses, port, public_key, display_name, session_id}]}`
(peer exchange для частичной связности) и `SYNC_REQUEST` (см. ниже).

Heartbeat: `PING` каждые 5 с, ответ `PONG` с тем же `request_id`; нет входящих данных 20 с → закрытие.
`GOODBYE` — штатное закрытие.

## 4. События (EVENT)

`EVENT {event: {...}}` — одно событие (структура ТЗ 8.1, `session_id` обязателен). Получатель:
1. размер ≤ 256 КБ, иначе отброс; 2. token bucket по `device_id` автора (100/с, burst 200) — превышение → отброс,
стабильное превышение по одному соединению → пир отключается на 30 с; 3. ограниченная очередь (2000) — переполнение → отброс + лог;
4. `event_id` уже есть → игнор; 5. новое → транзакция (lamport = max+1, INSERT, проекции, вектор) → переслать всем, кроме источника.
`EVENT_ACK` зарезервирован (не требуется: доставку гарантирует anti-entropy).

## 5. Синхронизация

Vector clock = `{device_id: непрерывный префикс device_seq}` в рамках `session_id`.

* `SYNC_REQUEST {session_id, vector}` → ответ `SYNC_RESPONSE {session_id, events[], more, vector}`.
  Ответ содержит события, у которых `device_seq > vector[device_id]`, отсортированные по `(device_id, device_seq)`,
  не более `sync_page_size` (200). **Пагинация**: `more = true` → запрашивающий, применив полученное,
  шлёт новый `SYNC_REQUEST` с обновлённым вектором. Обрыв в середине: применённые события считаются
  полученными (каждое — своя транзакция), остаток доберёт следующий запрос.
* `ANTI_ENTROPY_PROBE {session_id, vector}` — раз в 10–30 с случайному пиру. Получатель отвечает
  `SYNC_RESPONSE` (что не хватает отправителю) **и** шлёт свой `SYNC_REQUEST` (что не хватает ему).
  **Одновременный probe** двух узлов даёт два независимых обмена — это принятое решение; обмен идемпотентен.
* Событие, полученное через sync и оказавшееся новым, тоже пересылается дальше по gossip.

## 6. Файлы (отдельное TCP-соединение, модель pull)

Получатель инициирует соединение к владельцу файла (адрес = адрес живого управляющего соединения + его порт).

| Пакет | Направление | payload |
|---|---|---|
| `FILE_RESUME` | получатель → владелец | `file_id, transfer_id, have (hex-битмап чанков), session_id` |
| `FILE_OFFER` | владелец → получатель | манифест: `file_id, filename, size, chunk_size, chunk_count, full_sha256, chunk_hashes[]` |
| `FILE_REJECT` | владелец → получатель | `reason: NOT_FOUND | BUSY (retry_after)` |
| `FILE_ACCEPT` | получатель → владелец | `transfer_id, missing[]` — индексы недостающих чанков |
| `FILE_CHUNK` | владелец → получатель | бинарный фрейм `0x01 + index + data` |
| `FILE_CHUNK_ACK` | получатель → владелец | `transfer_id, received` — кумулятивно за раунд |
| `FILE_FINISH` | оба | владелец: «все запрошенные чанки отправлены»; получатель: `ok: true` — файл проверен и сохранён |
| `FILE_ABORT` | оба | `reason` |

Окно: владелец держит ≤ `ack_window` (16) неподтверждённых чанков; получатель подтверждает каждые
`ack_window/4` чанков и в конце раунда. Backpressure — `drain()` сокета + окно. Битый чанк не подтверждается и
запрашивается в следующем раунде `FILE_ACCEPT` (до 4 раундов). Затем полный SHA-256 → atomic rename в `files/`.
`FILE_OFFER` по управляющему соединению — «личная отправка»: получатель (при auto-accept) сам открывает
файловое соединение. Лимит одновременных исходящих — 3; лишние получают `BUSY` и повторяют через 10 с.

## 7. Discovery-пакет (UDP broadcast, порт 45820) и mDNS

```json
{"protocol_version": 1, "device_id": "...", "public_key": "...", "session_id": "...", "session_code": "K7F2-X9",
 "session_name": "...", "display_name": "...", "port": 45821, "addresses": ["192.168.1.25"]}
```
mDNS: сервис `_localclass._tcp.local.`, TXT: `did, pk, sid, code, sname, dn, v`.

## 8. Границы значений

| Параметр | Значение по умолчанию |
|---|---|
| пакет управляющего канала | ≤ 1 МиБ |
| событие | ≤ 256 КиБ |
| chunk_size / max | 1 МиБ / 4 МиБ |
| страница sync | 200 событий |
| событий/с на device_id | 100 |
| таймаут рукопожатия / файлового I/O | 10 с / 60 с |
