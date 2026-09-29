"""Фильтры водителя: вся Молдова, только ПМР, всё подряд или конкретный маршрут."""
from dataclasses import dataclass

from .parsing import Parsed
from .places import EU, MD, PMR, UA

ALL, MOLDOVA, PMR_ONLY, UKRAINE, EUROPE, ROUTE = "all", "md", "pmr", "ua", "eu", "route"


@dataclass
class Filter:
    kind: str                       # all | md | pmr | ua | eu | route
    from_place: str | None = None   # для route
    to_place: str | None = None     # для route
    both_ways: bool = True
    id: int | None = None

    def title(self) -> str:
        if self.kind == ALL:
            return "🌍 Все заявки"
        if self.kind == MOLDOVA:
            return "🇲🇩 По Молдове"
        if self.kind == PMR_ONLY:
            return "🔴 ПМР"
        if self.kind == UKRAINE:
            return "🇺🇦 Украина"
        if self.kind == EUROPE:
            return "🇪🇺 Европа"
        arrow = "⇄" if self.both_ways else "→"
        return f"🛣 {self.from_place or 'любой'} {arrow} {self.to_place or 'любой'}"


def _route_ok(frm_f, to_f, p: Parsed) -> bool:
    """Маршрут фильтра совпадает с заявкой; если в заявке указан только один
    конец (например, «кто едет в Кишинёв?»), достаточно совпадения этого конца."""
    if not p.has_route:
        return False
    from_ok = p.from_place is None or frm_f is None or p.from_place == frm_f
    to_ok = p.to_place is None or to_f is None or p.to_place == to_f
    hit = (frm_f and p.from_place == frm_f) or (to_f and p.to_place == to_f)
    return bool(from_ok and to_ok and hit)


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
        return f.both_ways and _route_ok(f.to_place, f.from_place, p)
    return False
