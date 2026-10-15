---
name: whoop-health
version: 0.3.1
type: extension
plugin_api: "2.0"
runtime: python3
entry: plugin.py
description: >
  Сбор данных со смарт-браслета WHOOP через официальный API v2:
  сон, recovery, strain, тренировки, HRV, пульс покоя, SpO2.
  Полный OAuth2 Authorization Code flow для первичной авторизации
  и автоматическое обновление токенов.
when_to_use: >
  Пользователь спрашивает о данных с WHOOP: сон, восстановление,
  нагрузка (strain), тренировки, HRV, пульс, SpO2.
  Примеры: «покажи recovery с WHOOP», «как я спал по WHOOP?»,
  «сводка здоровья WHOOP», «авторизуй WHOOP».
permissions: [tool, read_settings, net]
env_from_settings: [WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET]
requested_keys:
  - key: WHOOP_CLIENT_ID
    description: "Client ID приложения WHOOP (developer.whoop.com → My Apps)."
  - key: WHOOP_CLIENT_SECRET
    description: "Client Secret того же приложения WHOOP."
timeout_sec: 90
---

# WHOOP Health v0.3.1

Extension skill для Ouroboros — собирает данные со смарт-браслета WHOOP
через [официальный API v2](https://developer.whoop.com/api)
(`https://api.prod.whoop.com/developer/v2`; API v1 больше не поддерживается WHOOP).

## Аутентификация — OAuth2 Authorization Code Flow

### Первичная настройка

1. Зарегистрируйте приложение на [developer.whoop.com](https://developer.whoop.com)
   и укажите в нём redirect URI (по умолчанию `http://localhost:8080/callback`)
2. Получите `client_id` и `client_secret`
3. Положите их в секреты Ouroboros: `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`
4. Вызовите `whoop_auth_url` — получите URL для авторизации в браузере
5. Откройте URL, авторизуйтесь, скопируйте `code` из redirect
6. Вызовите `whoop_exchange_code` с полученным кодом
7. Готово — токены сохранены, все data-инструменты работают автоматически

### Обновление токенов

Access token обновляется автоматически через refresh token при получении
401/403 от API (scope `offline` запрашивается по умолчанию, без него WHOOP
не выдаёт refresh token). Токены сохраняются в файл `whoop_tokens.json`
в state-директории навыка (права 0600) и переживают рестарты.

## Инструменты

| Tool | Описание |
|------|----------|
| `whoop_auth_url` | Генерирует OAuth2 URL для авторизации в браузере |
| `whoop_exchange_code` | Обменивает authorization code на access + refresh tokens |
| `whoop_check` | Проверка подключения (GET /v2/user/profile/basic) |
| `whoop_summary` | Сводка за последние N дней: recovery, сон, strain. |
| `whoop_fetch` | Детальные данные по типу: RECOVERY, SLEEP, WORKOUT, CYCLE. |

## Данные и права

- Запросы идут только к `api.prod.whoop.com`; данные никуда, кроме ответа
  инструмента, не отправляются.
- Права: `tool`, `read_settings` (только два ключа выше), `net`.
- Зависимостей нет — только стандартная библиотека Python.
