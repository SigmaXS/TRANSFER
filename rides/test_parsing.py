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
    assert matches(Filter("md"), p)
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
    assert matches(Filter("ua"), u) and matches(Filter("md"), u) and not matches(Filter("eu"), u)
    e = parse_message("Caut loc Chișinău - Italia, plec luni")
    assert e.to_place == "Италия" and matches(Filter("eu"), e) and not matches(Filter("pmr"), e)
    print("Фильтры: OK")
    print(f"\nИтого: {len(CASES) - failed}/{len(CASES)} сообщений разобрано верно")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run() else 0)
