"""Проверка парсера на типичных сообщениях. Запуск: python -m rides.test_parsing"""
from rides.parsing import DRIVER, PASSENGER, parse_message
from rides.filters import Filter, matches

CASES = [
    # (текст, тип, откуда, куда)
    ("Ищу машину из Тирасполя в Кишинёв завтра в 8:00, 2 человека", PASSENGER, "Тирасполь", "Кишинёв"),
    ("Тирасполь - Кишинев сегодня 15:30 1 чел", PASSENGER, "Тирасполь", "Кишинёв"),
    ("Кто едет с Бендер на Кишинёв сейчас?", PASSENGER, "Бендеры", "Кишинёв"),
    ("Нужно уехать в Рыбницу из Тирасполя после обеда", PASSENGER, "Тирасполь", "Рыбница"),
    ("Еду из Кишинёва в Бельцы в 17:00, есть 3 места", DRIVER, "Кишинёв", "Бельцы"),
    ("Caut mașină Chișinău - Bălți mâine dimineață", PASSENGER, "Кишинёв", "Бельцы"),
    ("Chișinău-Iași locuri libere, mașina personală 24.06 la 8:00", DRIVER, "Кишинёв", "Яссы"),
    ("Cine merge din Comrat spre Chișinău azi?", PASSENGER, "Комрат", "Кишинёв"),
    ("Бендеры Кишинев", PASSENGER, "Бендеры", "Кишинёв"),
    ("Подвезите до аэропорта из Тирасполя в 5 утра", PASSENGER, "Тирасполь", "Аэропорт Кишинёва"),
    ("Возьму попутчиков Дубоссары → Тирасполь, выезд в 7:30", DRIVER, "Дубоссары", "Тирасполь"),
    ("Кишинёв в Одессу кто поедет на выходных?", PASSENGER, "Кишинёв", "Одесса"),
    ("Нужно 1 место\nКишинев - Бендеры\nСейчас 30.09", PASSENGER, "Кишинёв", "Бендеры"),
    ("Нужно место 1\nКишинев - Тирасполь\nНа ближайшее время", PASSENGER, "Кишинёв", "Тирасполь"),
    ("Есть 2 места Тирасполь - Кишинёв в 18:00", DRIVER, "Тирасполь", "Кишинёв"),
    ("Сегодня 30.09 могу сейчас могу в 04:00 с тирасполя в аэропорт", DRIVER, "Тирасполь", "Аэропорт Кишинёва"),
    ("1 октября (четверг)\nВыезжаю в 9:30 из Тирасполя (Балка) в Каменку.\nЕсть места\n077874154", DRIVER, "Тирасполь", "Каменка"),
    ("Завтра, 01.10 из Комрата до Кишинёва, северный автовокзал в 14-14-30\n060304870\nТатьяна", DRIVER, "Комрат", "Кишинёв"),
    ("Добрый день сегодня в 15:30/16:00 еду из Кишинева Комрат Чадыр- Лунга\n068154018", DRIVER, "Кишинёв", "Комрат"),
    ("Едет кто то в Кишинев с конгаза", PASSENGER, "Конгаз", "Кишинёв"),
    ("ТРАНСФЕР Кишинев-Одесса ежедневно 24/7 +37369123456 Viber", DRIVER, "Кишинёв", "Одесса"),
]


def run():
    failed = 0
    for text, kind, frm, to in CASES:
        p = parse_message(text)
        ok = (p.kind, p.from_place, p.to_place) == (kind, frm, to)
        failed += not ok
        mark = "OK  " if ok else "FAIL"
        print(f"{mark} {text[:55]:55} → {p.kind:9} {p.from_place} → {p.to_place}"
              f" | {p.when or ''} {p.time or ''} {p.date or ''}")
        if not ok:
            print(f"     ожидалось: {kind} {frm} → {to}")

    # Фильтры
    p = parse_message("Ищу машину из Тирасполя в Кишинёв завтра")
    assert matches(Filter("pmr"), p)
    assert not matches(Filter("md"), p)  # Тирасполь → Кишинёв — это ПМР → Молдова
    assert matches(Filter("route", "Тирасполь", "Кишинёв"), p)
    assert not matches(Filter("route", "Кишинёв", "Тирасполь", both_ways=False), p)
    assert matches(Filter("route", "Кишинёв", "Тирасполь", both_ways=True), p)
    assert not matches(Filter("route", "Тирасполь", "Бельцы"), p)
    q = parse_message("Кто едет из Бельц в Кишинев?")
    assert not matches(Filter("pmr"), q)
    assert matches(Filter("md"), q)
    r = parse_message("Тирасполь - Рыбница сегодня 1 чел")
    assert not matches(Filter("md"), r)
    assert matches(Filter("pmr"), r)
    u = parse_message("Кто едет из Кишинёва в Одессу завтра?")
    assert matches(Filter("ua"), u) and not matches(Filter("md"), u) and not matches(Filter("eu"), u)
    e = parse_message("Caut loc Chișinău - Italia, plec luni")
    assert e.to_place == "Италия" and matches(Filter("eu"), e) and not matches(Filter("pmr"), e)
    # Строгий маршрут: Кишинёв → Тирасполь не ловит Рыбницу и заявки без второго города
    kt = Filter("route", "Кишинёв", "Тирасполь", both_ways=True)
    assert not matches(kt, parse_message("Ищу машину Кишинёв - Рыбница сегодня"))
    assert not matches(kt, parse_message("Кто едет из Кишинёва сейчас?"))
    assert not matches(kt, parse_message("Кто едет в Тирасполь?"))
    assert matches(kt, parse_message("Кишинёв - Бендеры сейчас 1 чел"))      # Бендеры рядом с Тирасполем
    assert matches(kt, parse_message("Нужна машина из Тирасполя в Кишинёв"))  # обратно
    assert not matches(Filter("route", "Кишинёв", "Тирасполь", both_ways=False),
                       parse_message("Нужна машина из Тирасполя в Кишинёв"))
    assert matches(Filter("route", None, "Тирасполь"), parse_message("Кишинёв - Тирасполь 1 чел"))
    assert matches(Filter("route", None, None), parse_message("Кишинёв - Рыбница 1 чел"))
    # Направление бот определяет сам
    from rides.places import direction_label
    assert direction_label("Кишинёв", "Бельцы") == "по Молдове"
    assert direction_label("Кишинёв", "Рыбница") == "Молдова → ПМР"
    assert direction_label("Кишинёв", "Каменка") == "Молдова → ПМР"
    assert direction_label("Тирасполь", "Одесса") == "ПМР → Украина"
    assert direction_label(None, "Тирасполь") == "в ПМР"
    assert Filter("route", "Кишинёв", "Бельцы").title() == "🛣 Кишинёв ⇄ Бельцы (по Молдове)"
    assert not matches(Filter("md"), parse_message("Сегодня после 17.00 еду из Кишинева в Рыбницу, есть места"))
    assert not matches(Filter("md"), parse_message("Ищу машину Кишинёв - Тирасполь"))
    assert matches(Filter("md"), parse_message("Ищу машину Кишинёв - Комрат"))
    print("Фильтры: OK")
    print(f"\nИтого: {len(CASES) - failed}/{len(CASES)} сообщений разобрано верно")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run() else 0)
