"""
Следит за электронной очередью Паспортного сервиса (Варшава) и пишет в Telegram,
когда появляются свободные слоты.

Бот только СООБЩАЕТ о слотах. Записываться нужно самому по ссылке из сообщения
(сайт всё равно требует подтверждение через Дія / BankID).

Настройки берутся из переменных окружения:
  TELEGRAM_BOT_TOKEN  - токен бота от @BotFather
  TELEGRAM_CHAT_ID    - твой chat id
  TEST=1              - один раз проверить и прислать скриншот в любом случае
"""

import json
import os
import pathlib
import sys
import time

import requests
from playwright.sync_api import sync_playwright

URL = os.getenv("QUEUE_URL", "https://warszawa.pasport.org.ua/solutions/e-queue")
SERVICE = os.getenv("SERVICE_NAME", "Закордонний паспорт")
BUSY_TEXT = "всі місця зайняті"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TEST = os.getenv("TEST", "").lower() in ("1", "true", "yes")

# Сколько секунд работает один запуск и как часто он проверяет страницу.
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "180"))
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "90"))

# Не чаще чем раз в столько секунд повторять одно и то же уведомление.
THROTTLE = {
    "available": 10 * 60,
    "unclear": 30 * 60,
    "blocked": 6 * 60 * 60,
    "error": 6 * 60 * 60,
}

STATE_FILE = pathlib.Path("state.json")
SCREENSHOT = "page.png"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Выбирает услугу в выпадающем списке (если он обычный <select>).
JS_SELECT_SERVICE = """
(service) => {
  for (const s of document.querySelectorAll('select')) {
    const opt = [...s.options].find(o => o.textContent.includes(service));
    if (!opt) continue;
    if (s.value !== opt.value) {
      s.value = opt.value;
      s.dispatchEvent(new Event('input', {bubbles: true}));
      s.dispatchEvent(new Event('change', {bubbles: true}));
    }
    return true;
  }
  return false;
}
"""

# Ищет список "Обрати день" и возвращает даты, которые в нём есть.
JS_READ_DAYS = """
() => {
  const labelOf = (s) => {
    let t = [...(s.labels || [])].map(l => l.textContent).join(' ');
    if (!t.trim() && s.previousElementSibling) t = s.previousElementSibling.textContent;
    let p = s.parentElement;
    for (let i = 0; i < 2 && p && !t.trim(); i++, p = p.parentElement) {
      t = p.textContent.slice(0, 200);
    }
    return (t || '').toLowerCase();
  };
  for (const s of document.querySelectorAll('select')) {
    if (!labelOf(s).includes('день')) continue;
    return [...s.options]
      .filter(o => o.value && !o.disabled && !o.textContent.toLowerCase().includes('обрати'))
      .map(o => o.textContent.trim());
  }
  return null;
}
"""


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_notified": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def send(text, photo=None):
    if not TOKEN or not CHAT_ID:
        print("[telegram не настроен] " + text)
        return
    base = f"https://api.telegram.org/bot{TOKEN}"
    try:
        if photo and os.path.exists(photo):
            with open(photo, "rb") as f:
                r = requests.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": CHAT_ID, "caption": text[:1000]},
                    files={"photo": f},
                    timeout=30,
                )
        else:
            r = requests.post(
                f"{base}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
                timeout=30,
            )
        if r.status_code != 200:
            print("Telegram ответил ошибкой:", r.status_code, r.text[:300])
    except Exception as e:
        print("Не удалось отправить в Telegram:", e)


def check_once(browser):
    """Возвращает (статус, список дат). Статусы: busy, available, unclear, blocked."""
    context = browser.new_context(
        locale="uk-UA",
        timezone_id="Europe/Warsaw",
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 1600},
    )
    page = context.new_page()
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)

        title = (page.title() or "").lower()
        if "just a moment" in title or "attention required" in title:
            page.screenshot(path=SCREENSHOT)
            return "blocked", []

        page.evaluate(JS_SELECT_SERVICE, SERVICE)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        page.screenshot(path=SCREENSHOT, full_page=False)
        text = page.inner_text("body").lower()

        if BUSY_TEXT in text:
            return "busy", []

        days = page.evaluate(JS_READ_DAYS)
        if days:
            return "available", days
        return "unclear", []
    finally:
        context.close()


def message_for(status, days):
    if status == "available":
        shown = ", ".join(days[:8]) + (" ..." if len(days) > 8 else "")
        return f"Есть свободные слоты в Паспортном сервисе!\nДаты: {shown}\nЗаписывайся: {URL}"
    if status == "unclear":
        return (
            "Надпись «всі місця зайняті» пропала, но даты прочитать не удалось.\n"
            f"Возможно, появились слоты. Проверь: {URL}"
        )
    if status == "blocked":
        return "Сайт показывает защиту Cloudflare и не пускает бота. Проверки пока не работают."
    return ""


def main():
    state = load_state()
    last = state.setdefault("last_notified", {})
    started = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            while True:
                try:
                    status, days = check_once(browser)
                except Exception as e:
                    status, days = "error", []
                    print("Ошибка проверки:", repr(e))

                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"{stamp} статус: {status} {days if days else ''}")
                state["last_status"] = status
                state["last_check"] = stamp

                if TEST:
                    send(
                        f"Тест: бот работает. Сейчас статус: {status}"
                        + (f", даты: {', '.join(days[:8])}" if days else ""),
                        photo=SCREENSHOT,
                    )
                    break

                if status in THROTTLE:
                    now = time.time()
                    if now - last.get(status, 0) >= THROTTLE[status]:
                        text = message_for(status, days) or "Проверка страницы упала с ошибкой."
                        send(text, photo=SCREENSHOT if status != "error" else None)
                        last[status] = now
                elif status == "busy":
                    # Слоты закончились: в следующий раз сообщим сразу, без паузы.
                    last.pop("available", None)
                    last.pop("unclear", None)

                save_state(state)
                if time.time() - started + INTERVAL_SECONDS > LOOP_SECONDS:
                    break
                time.sleep(INTERVAL_SECONDS)
        finally:
            browser.close()

    save_state(state)


if __name__ == "__main__":
    sys.exit(main())
