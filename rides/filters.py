"""Фильтры: откуда (город + всё рядом) → куда (любое / регион / город), либо целый регион."""
import os
from dataclasses import dataclass

from .parsing import Parsed
from .places import COORDS, EU, MD, PMR, UA, distance_km, region_of

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
            return "🇲🇩 Вся Молдова"
        if self.kind == PMR_ONLY:
            return "🔴 Все заявки ПМР"
        if self.kind == UKRAINE:
            return "🇺🇦 Украина"
        if self.kind == EUROPE:
            return "🇪🇺 Европа"
        frm = self._label(self.from_place, REGION_FROM_LABEL, "откуда угодно")
        to = self._label(self.to_place, REGION_TO_LABEL, "куда угодно")
        arrow = "⇄" if self.both_ways and self.from_place and self.to_place else "→"
        return f"🛣 {frm} {arrow} {to}"

    @staticmethod
    def _label(spec, region_labels, empty):
        if spec is None:
            return empty
        if spec in region_labels:
            return region_labels[spec]
        return f"{spec} и рядом" if spec in COORDS else spec


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
    """Если в заявке указан только один конец («кто едет в Кишинёв?»), достаточно, чтобы
    совпал он, а второй конец фильтра был бы не противоречащим."""
    if not p.has_route:
        return False
    fr, to = spec_matches(frm_f, p.from_place), spec_matches(to_f, p.to_place)
    if fr is False or to is False:
        return False
    if frm_f is None and to_f is None:
        return True
    return (frm_f is not None and fr is True) or (to_f is not None and to is True)


def matches(f: Filter, p: Parsed) -> bool:
    if not p.places:
        return False
    if f.kind == ALL:
        return True
    if f.kind == MOLDOVA:
        return MD in p.regions
    if f.kind == PMR_ONLY:
        return PMR in p.regions
    if f.kind == UKRAINE:
        return UA in p.regions
    if f.kind == EUROPE:
        return EU in p.regions
    if f.kind == ROUTE:
        if _route_ok(f.from_place, f.to_place, p):
            return True
        return bool(f.both_ways and f.from_place and f.to_place
                    and _route_ok(f.to_place, f.from_place, p))
    return False
