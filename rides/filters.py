"""Фильтры: откуда (город + всё рядом) → куда (любое / регион / город), либо целый регион."""
import os
from dataclasses import dataclass

from .parsing import Parsed
from .places import EU, MD, PMR, UA, direction_label, distance_km, region_of

ALL, MOLDOVA, PMR_ONLY, UKRAINE, EUROPE, ROUTE = "all", "md", "pmr", "ua", "eu", "route"

NEARBY_KM = float(os.environ.get("RIDES_NEARBY_KM", "25"))

# В маршруте вместо города можно указать целый регион: "@pmr", "@md", "@ua", "@eu"
REGION_SPECS = {"@pmr": PMR, "@md": MD, "@ua": UA, "@eu": EU}
REGION_TO_LABEL = {"@pmr": "в ПМР", "@md": "по Молдове", "@ua": "в Украину", "@eu": "в Европу"}
REGION_FROM_LABEL = {"@pmr": "из ПМР", "@md": "из Молдовы", "@ua": "из Украины", "@eu": "из Европы"}


@dataclass
class Filter:
    kind: str                       # all | md | pmr | ua | eu | route
    from_place: str | None = None   # для route: город, "@регион" или None (любой)
    to_place: str | None = None
    both_ways: bool = True
    id: int | None = None

    def title(self) -> str:
        if self.kind == ALL:
            return "🌍 Все"
        if self.kind == MOLDOVA:
            return "🇲🇩 По Молдове (без ПМР)"
        if self.kind == PMR_ONLY:
            return "🔴 Все заявки ПМР"
        if self.kind == UKRAINE:
            return "🇺🇦 Украина"
        if self.kind == EUROPE:
            return "🇪🇺 Европа"
        frm = self._label(self.from_place, REGION_FROM_LABEL, "откуда угодно")
        to = self._label(self.to_place, REGION_TO_LABEL, "куда угодно")
        arrow = "⇄" if self.both_ways and self.from_place and self.to_place else "→"
        title = f"🛣 {frm} {arrow} {to}"
        direction = self.direction()
        return f"{title} ({direction})" if direction else title

    def direction(self) -> str | None:
        """«по Молдове», «Молдова → ПМР» — бот определяет сам по выбранным городам."""
        if self.kind != ROUTE:
            return None
        frm = None if (self.from_place or "").startswith("@") else self.from_place
        to = None if (self.to_place or "").startswith("@") else self.to_place
        if not frm and not to:
            return None
        return direction_label(frm, to)

    @staticmethod
    def _label(spec, region_labels, empty):
        if spec is None:
            return empty
        if spec in region_labels:
            return region_labels[spec]
        return spec


def spec_matches(spec: str | None, place: str | None) -> bool | None:
    """True — подходит, False — нет, None — в заявке этот конец не указан."""
    if spec is None:
        return True
    if place is None:
        return None
    if spec in REGION_SPECS:
        return region_of(place) == REGION_SPECS[spec]
    if place == spec:
        return True
    d = distance_km(spec, place)
    return d is not None and d <= NEARBY_KM


def _route_ok(frm_f, to_f, p: Parsed) -> bool:
    """Строгое совпадение: каждый конец, заданный в фильтре, должен быть указан в заявке
    и совпасть (сам город или место рядом). Заявка «Кишинёв → ?» или «Кишинёв → Рыбница»
    под фильтр «Кишинёв → Тирасполь» не попадает."""
    if not p.has_route:
        return False
    fr, to = spec_matches(frm_f, p.from_place), spec_matches(to_f, p.to_place)
    return fr is True and to is True


def _carrier_ok(f: Filter, p: Parsed) -> bool:
    """Перевозчики и такси ездят в обе стороны и по всем городам из объявления:
    подходит, если в объявлении есть и «откуда», и «куда» фильтра."""
    def any_hit(spec):
        return spec is None or any(spec_matches(spec, place) for place in p.places)
    return bool((f.from_place or f.to_place) and any_hit(f.from_place) and any_hit(f.to_place))


def matches(f: Filter, p: Parsed) -> bool:
    if not p.places:
        return False
    if f.kind == ALL:
        return True
    if f.kind == MOLDOVA:
        # Только поездки внутри Молдовы: Кишинёв → Рыбница — это уже «Молдова → ПМР»
        return p.regions == {MD}
    if f.kind == PMR_ONLY:
        return PMR in p.regions
    if f.kind == UKRAINE:
        return UA in p.regions
    if f.kind == EUROPE:
        return EU in p.regions
    if f.kind == ROUTE:
        if _route_ok(f.from_place, f.to_place, p):
            return True
        if p.is_ad and _carrier_ok(f, p):
            return True
        return bool(f.both_ways and f.from_place and f.to_place
                    and _route_ok(f.to_place, f.from_place, p))
    return False
