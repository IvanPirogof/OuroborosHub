---
name: telegram-bridge-handmade
description: Telegram duplex bridge — incoming via Host Service injection, outgoing text/photo/file mirrored to chat
version: 1.3.0
type: extension
runtime: python3
entry: plugin.py
permissions: [net, read_settings, inject_chat, subscribe_event, fs, tool]
subscribe_events: [chat.outbound, chat.typing, chat.photo]
env_from_settings: [TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_API_ID, TELEGRAM_API_HASH]
when_to_use: Relay Telegram messages into the Ouroboros chat, mirror outgoing replies, and deliver produced files to Telegram.
timeout_sec: 60
---

# Telegram Bridge

Бидуплексный мост между Telegram и чатом Ouroboros.

**Входящие**: фоновый поток поллит Telegram `getUpdates` (timeout=15s) и
инжектит каждое сообщение в чат Ouroboros через Host Service
`POST /chat/inject` (source=`skill:telegram-bridge-handmade`, sender label обернут
`[Telegram] @username`). Владелец видит входящие как обычные сообщения в
Web UI и может отвечать им напрямую.

Поддерживаемые типы входящих сообщений:
- Текст и подписи к медиа (text, caption)
- Документы (PDF и любые файлы) — скачиваются через `getFile` в skill state dir (`data/state/skills/telegram-bridge-handmade/incoming/telegram/`)
- Аудио (audio) — скачиваются в skill state dir
- Голосовые сообщения (voice) — скачиваются в skill state dir
- Фото (photo) — скачиваются в skill state dir (v1.2.0: добавлено, крупнейший размер из набора)
- Видео (video) — скачиваются в skill state dir (v1.2.0: добавлено)

**Исходящие**: как только агент отвечает в чат, событие `chat.outbound`
публикуется на event bus. Навык подписан на это событие (`subscribe_event`)
и отправляет текст в Telegram через Bot API `sendMessage` (чат из
`TELEGRAM_CHAT_ID`), дробя на сегменты ≤ 4096 символов. Тем самым каждое
сообщение агента дублируется в Telegram.

**Исходящие фото (v1.2.0)**: когда агент доставляет картинку через
`send_photo`, на event bus публикуется событие `chat.photo` (payload несёт
`image_base64` + `mime` + `caption`). Навык подписан на него и отправляет
фото в Telegram через Bot API `sendPhoto` (multipart/form-data, только
stdlib) в тот же `TELEGRAM_CHAT_ID`. Это закрывает разрыв, когда картинка
доходит до Web UI, но не зеркалится в Telegram. Фото из Telegram-транспорта
пропускаются (нет эхо-цикла).

**Исходящие файлы (v1.3.0)**: навык регистрирует namespaced-инструмент
`send_file` (permission `tool`), и агент может доставить ЛЮБОЙ локальный файл
в чат владельца как настоящий документ Telegram — Bot API `sendDocument`,
multipart/form-data, только stdlib.

```
send_file(path="/home/rdp/Ouroboros/data/task_drives/<id>/report.xlsx",
          caption="сводная ведомость", filename="report.xlsx")
```

- Лимит Bot API `sendDocument` — **50 МБ**. Файл больше лимита не обрезается
  молча: инструмент возвращает явную ошибку с действием (уменьшить файл,
  разбить, прислать ссылку). MTProto-загрузки в этой версии нет.
- **Только tool, без HTTP-route.** Route сделал бы «загрузи любой локальный
  путь в Telegram» доступным из browser/API-поверхности, то есть универсальным
  примитивом эксфильтрации. Инструмент вызывается только из агентского цикла,
  а получатель жёстко привязан к `TELEGRAM_CHAT_ID` владельца.
- Путь читается caller-ом (агенту нужно отправлять файлы, которые он только
  что создал в `task_drive`/`artifact_store`/`user_files`), поэтому
  skill-dir confinement здесь неприменим; вместо него — явный denylist
  чувствительных путей (`settings.json`, `auth_token.json`, `*.pem`, `*.key`,
  `mtproto.session`, `.env*`, `credentials.json`, `/data/state/`, `/.ssh/`,
  `/.aws/`, `/.gnupg/`). Инъекция в промпт не сможет заставить мост выгрузить
  секреты владельца. Проверка идёт и по исходному пути, и по
  `os.path.realpath(...)`: симлинк на секрет (`report.xlsx -> settings.json`) и
  `..`-обход denylist не проходят — иначе защита обходилась бы одной ссылкой.
- Caption длиннее 1024 символов обрезается **с видимой пометкой** в результате
  инструмента, а не молча.

**MTProto fallback (v1.1.0)**: Bot API `getFile` жёстко ограничен 20 МБ.
Документы/аудио/голосовые больше этого лимита скачиваются через Telethon
(MTProto, до 2 ГБ) по тому же bot-токену. Требуются `TELEGRAM_API_ID` /
`TELEGRAM_API_HASH` (my.telegram.org). Если ключи не заданы или telethon
не установлен — файл не падает молча: в чат приходит явное сообщение об
ошибке. Сессия MTProto хранится в skill state dir (`mtproto.session`),
скачивание — в тот же `incoming/telegram/`, с той же политикой очистки.

Регистрация **fail-closed**: при пустом `TELEGRAM_BOT_TOKEN`, ошибке `getMe`
или пустом/нечисловом `TELEGRAM_CHAT_ID` навык поднимает
`ExtensionRegistrationError` — владелец видит `load_error` в Skills UI, а не
тихую «оффлайн»-навычку без работающего моста.

Процесс живёт внутри серверного процесса Ouroboros: никакой отдельной
задачи-воркера, перезапускается вместе со сервером на VPS. Никаких файловых
посредников — только Host Service inject + Telegram Bot API.

## Почему in-process (а не companion-процесс)

- Requirement владельца: "система должна перезапускаться полностью, а не
  отдельными компонентами". In-process extension запускается в серверном
  процессе и живёт при нём — не умирает с задачей и не требует ручного
  подъёма.
- Избегаем внешней websocket-зависимости для подписки на `chat.outbound`:
  событие получаем через PluginAPI `subscribe_event` прямо в серверном
  процессе.
- Хост сам рестартирует навыки и падает вместе с сервером. Предсказуемо.

## Операционные заметки (для владельца и для будущего меня)

- **Правка payload = потеря грантов.** Гранты и вердикт ревью привязаны к
  content-hash. Любое изменение `plugin.py`/`SKILL.md` делает ревью stale и
  обнуляет `granted_keys`/`granted_permissions` — мост не поднимется, пока в
  Skills UI не будет заново нажат Grant. Порядок безопасного обновления:
  правка → `skill_preflight` → `skill_review` → грант владельца → проверка,
  что мост снова live.
- **Не поднимать второй инстанс моста на том же bot-токене.** `getUpdates`
  имеет один offset на бота: два поллера начнут «перетягивать» апдейты друг
  у друга. Для второго чата/пользователя нужен отдельный бот-токен.
- Реальная доставка файлов проверена до релиза автономным harness-скриптом,
  который вызывает `_send_document()` из этого же `plugin.py` с фейковым
  `api` (файл действительно ушёл в чат владельца) — потому что живой навык до
  повторного гранта недоступен.
