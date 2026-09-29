"""Справочник населённых пунктов Молдовы и ПМР + поиск их в тексте.

Каждое место: регион (md / pmr / abroad) и список «основ» названия.
Основа ищется как начало слова, поэтому «кишин» ловит Кишинёв, Кишинева,
Кишиневе, Кишинёву. Основы с префиксом "=" ищутся только целым словом
(нужно для слов, которые совпадают с обычными: «сорок» = 40).
Названия на русском, румынском и латинице; диакритика (ă, ș, ț, ё, й)
убирается при нормализации, так что «Bălți» и «balti» — одно и то же.
"""
import re
import unicodedata

MD, PMR, ABROAD = "md", "pmr", "abroad"
REGION_NAMES = {MD: "Молдова", PMR: "ПМР", ABROAD: "за рубеж"}

PLACES: dict[str, tuple[str, list[str]]] = {
    # --- Приднестровье ---
    "Тирасполь": (PMR, ["тирасп", "tiraspol"]),
    "Бендеры": (PMR, ["бендер", "тигин", "bender", "tighin"]),
    "Рыбница": (PMR, ["рыбниц", "ribnit", "rabnit", "rîbnit"]),
    "Дубоссары": (PMR, ["дубосс", "дубэсар", "dubasar", "dubossar"]),
    "Слободзея": (PMR, ["слободз", "slobozi"]),
    "Григориополь": (PMR, ["григориоп", "grigoriopol"]),
    "Каменка": (PMR, ["каменк", "camenc"]),
    "Днестровск": (PMR, ["днестровск", "dnestrovsk"]),
    "Первомайск": (PMR, ["первомайск", "pervomaisk"]),
    "Парканы": (PMR, ["паркан", "parcan"]),
    "Суклея": (PMR, ["сукле", "sucle"]),
    # --- Молдова ---
    "Кишинёв": (MD, ["кишин", "chisin", "kishin", "kisin"]),
    "Аэропорт Кишинёва": (MD, ["аэропорт", "aeroport", "airport"]),
    "Бельцы": (MD, ["бельц", "balti", "belts"]),
    "Кагул": (MD, ["кагул", "cahul"]),
    "Комрат": (MD, ["комрат", "comrat"]),
    "Оргеев": (MD, ["оргее", "орхе", "orhei"]),
    "Унгены": (MD, ["унген", "unghen"]),
    "Сороки": (MD, ["=сороки", "=сороках", "=сорокам", "soroc"]),
    "Флорешты": (MD, ["флорешт", "florest"]),
    "Фалешты": (MD, ["фалешт", "falest"]),
    "Хынчешты": (MD, ["хынчешт", "хинчешт", "hincest", "hancest"]),
    "Каушаны": (MD, ["каушан", "causen"]),
    "Чимишлия": (MD, ["чимишли", "cimisli"]),
    "Анений Ной": (MD, ["анени", "anenii"]),
    "Яловены": (MD, ["яловен", "ialoven"]),
    "Страшены": (MD, ["страшен", "strasen"]),
    "Криуляны": (MD, ["криулян", "criulen"]),
    "Дрокия": (MD, ["дроки", "drochi"]),
    "Единцы": (MD, ["единц", "edinet"]),
    "Бричаны": (MD, ["бричан", "bricen"]),
    "Окница": (MD, ["окниц", "ocnit"]),
    "Глодяны": (MD, ["глодян", "gloden"]),
    "Рышканы": (MD, ["рышкан", "рискан", "riscan"]),
    "Сынжерей": (MD, ["сынжере", "синжере", "singere", "sangere"]),
    "Теленешты": (MD, ["теленешт", "telenest"]),
    "Шолданешты": (MD, ["шолданешт", "soldanest"]),
    "Резина": (MD, ["=rezina"]),  # кириллицу не берём: «резина» = шины
    "Калараш": (MD, ["калараш", "calaras"]),
    "Ниспорены": (MD, ["ниспорен", "nisporen"]),
    "Леова": (MD, ["леов", "leova"]),
    "Кантемир": (MD, ["кантемир", "cantemir"]),
    "Тараклия": (MD, ["таракли", "taracli"]),
    "Вулканешты": (MD, ["вулканешт", "vulcanest"]),
    "Чадыр-Лунга": (MD, ["чадыр", "ceadir", "ciadir"]),
    "Басарабяска": (MD, ["басарабяск", "бессарабк", "basarabeasc"]),
    "Штефан-Водэ": (MD, ["штефан-вод", "штефан вод", "stefan-vod", "stefan vod"]),
    "Дондюшаны": (MD, ["дондюшан", "dondusen"]),
    "Паланка": (MD, ["паланк", "palanc"]),
    # --- За рубежом (частые направления) ---
    "Одесса": (ABROAD, ["одесс", "одес", "odes"]),
    "Яссы": (ABROAD, ["ясс", "iasi"]),
    "Бухарест": (ABROAD, ["бухарест", "bucurest", "buharest", "bucharest"]),
    "Киев": (ABROAD, ["=киев", "=киева", "=киеве", "kiev", "kyiv"]),
}


def normalize(text: str) -> str:
    """Нижний регистр + убрать диакритику (ё→е, й→и, ă→a, ș→s, ț→t)."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text


def _build_patterns():
    patterns = []
    for name, (region, aliases) in PLACES.items():
        for alias in aliases:
            exact = alias.startswith("=")
            stem = normalize(alias.lstrip("="))
            stem_re = re.escape(stem).replace(r"\ ", r"[\s\-]")
            tail = r"(?![\w])" if exact else r"\w*"
            patterns.append((re.compile(r"(?<![\w])" + stem_re + tail), name))
    return patterns


_PATTERNS = _build_patterns()


def region_of(place: str | None) -> str | None:
    return PLACES[place][0] if place in PLACES else None


def find_places(text: str, already_normalized: bool = False):
    """Все упоминания мест в тексте: список (start, end, название) по порядку."""
    norm = text if already_normalized else normalize(text)
    hits = []
    for rx, name in _PATTERNS:
        for m in rx.finditer(norm):
            hits.append((m.start(), m.end(), name))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    result, last_end = [], -1
    for start, end, name in hits:
        if start >= last_end:  # убираем пересечения
            result.append((start, end, name))
            last_end = end
    return result


def resolve_place(text: str) -> str | None:
    """Одно название, введённое пользователем, → каноническое имя."""
    found = find_places(text)
    return found[0][2] if found else None


# Популярные точки для кнопок в боте
POPULAR = [
    "Кишинёв", "Тирасполь", "Бендеры", "Бельцы", "Рыбница", "Дубоссары",
    "Комрат", "Кагул", "Оргеев", "Унгены", "Слободзея", "Григориополь",
    "Аэропорт Кишинёва", "Одесса", "Яссы",
]
