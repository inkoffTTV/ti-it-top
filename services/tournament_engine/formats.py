"""Форматы турнира и их реестр.

Сервис не содержит веток «если SE, иначе DE»: он берёт формат из FORMATS и
вызывает общий интерфейс. Сетки описываются графом (plan.MatchSpec со ссылками
winner/loser), продвижение по графу общее для всех форматов. Круговая система,
швейцарка, группы и GSL подключаются сюда же новыми классами (релиз 5.4).

Правила (ресёрч, см. docs/TOURNAMENT_ENGINE_ARCHITECTURE.md):
* SE: n−1 матчей (+ матч за 3-е место по желанию).
* DE: нижняя сетка чередует раунды «выжившие против выбывших из верхней» и
  «выжившие между собой»; порядок выбывших разворачивается через раунд, чтобы
  не было немедленных реваншей; гранд-финал; перезапуск — опционально.
"""
from __future__ import annotations

from typing import Any

from services.tournament_engine.plan import MatchSpec, Src, loser, team, winner
from services.tournament_engine.series import validate_bo


def _round_name(match_count: int, final_name: str = "Финал") -> str:
    if match_count == 1:
        return final_name
    if match_count == 2:
        return "Полуфинал"
    return f"1/{match_count} финала"


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class StageFormat:
    key = ""
    title = ""
    min_teams = 2
    brackets: dict[str, str] = {}
    uses_slots = True        # сетка из слотов посева (SE/DE); иначе build_table()
    allow_draw = False       # ничья в серии (BO2 1:1) — только групповые форматы
    dynamic_rounds = False   # туры создаются по ходу (швейцарка)
    table = False            # есть турнирная таблица (standings)
    standings_mode = ""      # rr | gsl | swiss

    def normalize_settings(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        raw = dict(raw or {})
        return {
            "bo_default": validate_bo(raw.get("bo_default", 1)),
            "bo_final": validate_bo(raw.get("bo_final", raw.get("bo_default", 1))),
        }

    def bracket_title(self, key: str) -> str:
        return self.brackets.get(key, key)

    def feeds_playoffs(self, settings: dict[str, Any]) -> bool:
        return False

    def build(self, slots: list[int | None], settings: dict[str, Any]) -> list[MatchSpec]:
        raise NotImplementedError

    def build_table(self, ordered_ids: list[int], settings: dict[str, Any]) -> tuple[list[MatchSpec], dict[int, dict[str, Any]], dict[str, Any]]:
        """Для табличных форматов: (матчи, {team_id: {"group": ...}}, доп. настройки этапа)."""
        raise NotImplementedError

    def qualifiers(self, standings: list[dict[str, Any]], settings: dict[str, Any]) -> list[int]:
        """Команды, выходящие в плей-офф, в порядке посева."""
        return []

    def next_round(self, state: dict[str, Any]) -> tuple[list[MatchSpec], list[int]] | None:
        """Следующий тур динамического формата: (матчи, команды с BYE) или None — этап завершён."""
        return None

    def followups(self, match_key: str, winner_slot: int, settings: dict[str, Any]) -> list[MatchSpec]:
        """Матчи, которые появляются только после результата (перезапуск гранд-финала)."""
        return []

    def followup_keys(self, match_key: str) -> list[str]:
        return []

    def describe(self, settings: dict[str, Any]) -> str:
        return self.title


def _upper_rounds(slots: list[int | None], settings: dict[str, Any], *, bracket: str, final_name: str,
                  final_is_series_final: bool) -> tuple[list[MatchSpec], int]:
    size = len(slots)
    rounds = size.bit_length() - 1
    specs: list[MatchSpec] = []
    prefix = "U" if bracket == "upper" else "R"
    for r in range(1, rounds + 1):
        count = size >> r
        for p in range(count):
            if r == 1:
                s1, s2 = team(slots[2 * p]), team(slots[2 * p + 1])
            else:
                s1, s2 = winner(f"{prefix}{r - 1}-{2 * p + 1}"), winner(f"{prefix}{r - 1}-{2 * p + 2}")
            is_last = count == 1
            bo = settings["bo_final"] if (is_last and final_is_series_final) else settings["bo_default"]
            specs.append(MatchSpec(
                key=f"{prefix}{r}-{p + 1}", bracket=bracket, round_no=r, position=p + 1,
                round_name=_round_name(count, final_name), best_of=bo,
                phase=(2 * r - 1) if bracket == "upper" else r, src1=s1, src2=s2,
            ))
    return specs, rounds


class SingleElimination(StageFormat):
    key = "single_elim"
    title = "Single elimination (олимпийская система)"
    min_teams = 2
    brackets = {"main": "Плей-офф", "third": "Матч за 3-е место"}

    def normalize_settings(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        base = super().normalize_settings(raw)
        base["third_place"] = _bool((raw or {}).get("third_place"), False)
        return base

    def build(self, slots: list[int | None], settings: dict[str, Any]) -> list[MatchSpec]:
        specs, rounds = _upper_rounds(slots, settings, bracket="main", final_name="Финал", final_is_series_final=True)
        if settings.get("third_place") and rounds >= 2:
            specs.append(MatchSpec(
                key="3RD", bracket="third", round_no=rounds, position=1, round_name="Матч за 3-е место",
                best_of=settings["bo_default"], phase=rounds - 0.5,
                src1=loser(f"R{rounds - 1}-1"), src2=loser(f"R{rounds - 1}-2"),
            ))
        return specs

    def describe(self, settings: dict[str, Any]) -> str:
        return f"Single elimination · BO{settings['bo_default']}, финал BO{settings['bo_final']}" + (
            " · матч за 3-е место" if settings.get("third_place") else "")


class DoubleElimination(StageFormat):
    key = "double_elim"
    title = "Double elimination (верхняя и нижняя сетка)"
    min_teams = 3
    brackets = {"upper": "Верхняя сетка", "lower": "Нижняя сетка", "final": "Гранд-финал"}

    def normalize_settings(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        base = super().normalize_settings(raw)
        base["grand_final_reset"] = _bool((raw or {}).get("grand_final_reset"), False)
        return base

    def build(self, slots: list[int | None], settings: dict[str, Any]) -> list[MatchSpec]:
        size = len(slots)
        k = size.bit_length() - 1
        if k < 2:
            raise ValueError("Для double elimination нужны минимум 3 команды.")
        upper, _ = _upper_rounds(slots, settings, bracket="upper", final_name="Финал верхней сетки",
                                 final_is_series_final=False)
        lower: list[MatchSpec] = []
        bo = settings["bo_default"]
        lower_rounds_total = 2 * k - 2

        def lname(no: int) -> str:
            return "Финал нижней сетки" if no == lower_rounds_total else f"Раунд {no}"

        # L1: проигравшие первого раунда верхней сетки играют между собой.
        l_no = 1
        prev: list[Src] = []
        for p in range(size // 4):
            key = f"L1-{p + 1}"
            lower.append(MatchSpec(key=key, bracket="lower", round_no=1, position=p + 1, round_name=lname(1),
                                   best_of=bo, phase=2, src1=loser(f"U1-{2 * p + 1}"), src2=loser(f"U1-{2 * p + 2}")))
            prev.append(winner(key))
        for r in range(2, k + 1):
            drops = [loser(f"U{r}-{j + 1}") for j in range(size >> r)]
            if r % 2 == 0:
                drops.reverse()  # разворот через раунд — против немедленных реваншей
            l_no += 1
            nxt: list[Src] = []
            for i, (survivor, dropped) in enumerate(zip(prev, drops)):
                key = f"L{l_no}-{i + 1}"
                lower.append(MatchSpec(key=key, bracket="lower", round_no=l_no, position=i + 1, round_name=lname(l_no),
                                       best_of=bo, phase=2 * r, src1=survivor, src2=dropped))
                nxt.append(winner(key))
            prev = nxt
            if r < k:
                l_no += 1
                nxt = []
                for i in range(len(prev) // 2):
                    key = f"L{l_no}-{i + 1}"
                    lower.append(MatchSpec(key=key, bracket="lower", round_no=l_no, position=i + 1, round_name=lname(l_no),
                                           best_of=bo, phase=2 * r + 0.5, src1=prev[2 * i], src2=prev[2 * i + 1]))
                    nxt.append(winner(key))
                prev = nxt
        final = MatchSpec(key="GF", bracket="final", round_no=1, position=1, round_name="Гранд-финал",
                          best_of=settings["bo_final"], phase=2 * k + 1, src1=winner(f"U{k}-1"), src2=prev[0])
        # Порядок зависимостей: верхняя сетка раньше нижней в пределах одной фазы.
        ordered = sorted(upper + lower, key=lambda s: (s.phase, s.bracket != "upper", s.position))
        return ordered + [final]

    def followups(self, match_key: str, winner_slot: int, settings: dict[str, Any]) -> list[MatchSpec]:
        if match_key == "GF" and settings.get("grand_final_reset") and int(winner_slot) == 2:
            return [MatchSpec(key="GF2", bracket="final", round_no=2, position=1,
                              round_name="Гранд-финал · перезапуск", best_of=settings["bo_final"], phase=99,
                              src1=Src("slot", ("GF", 1)), src2=Src("slot", ("GF", 2)))]
        return []

    def followup_keys(self, match_key: str) -> list[str]:
        return ["GF2"] if match_key == "GF" else []

    def describe(self, settings: dict[str, Any]) -> str:
        return (f"Double elimination · BO{settings['bo_default']}, гранд-финал BO{settings['bo_final']}"
                + (" с перезапуском" if settings.get("grand_final_reset") else ""))


# ---------------------------------------------------------------------------
# Табличные форматы: круговая система, группы (круговые / GSL), швейцарка.
# ---------------------------------------------------------------------------
PLAYOFF_FORMATS = ("single_elim", "double_elim")
LETTERS = "ABCDEFGHIJKLMNOP"


def _int_setting(raw: dict[str, Any], name: str, default: int, lo: int, hi: int, label: str) -> int:
    value = raw.get(name, default)
    try:
        number = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        raise ValueError(f"{label}: нужно целое число.") from None
    if not lo <= number <= hi:
        raise ValueError(f"{label}: от {lo} до {hi}.")
    return number


def _playoff_settings(raw: dict[str, Any], *, allow_none: bool) -> dict[str, Any]:
    fmt = str(raw.get("playoff_format") or ("none" if allow_none else "single_elim"))
    if fmt not in PLAYOFF_FORMATS + (("none",) if allow_none else ()):
        raise ValueError("Формат плей-офф: single или double elimination.")
    return {
        "playoff_format": fmt,
        "playoff_bo_default": validate_bo(raw.get("playoff_bo_default", 3)),
        "playoff_bo_final": validate_bo(raw.get("playoff_bo_final", raw.get("playoff_bo_default", 3))),
        "grand_final_reset": _bool(raw.get("grand_final_reset"), False),
        "third_place": _bool(raw.get("third_place"), False),
    }


def snake_groups(ordered_ids: list[int], count: int) -> dict[str, list[int]]:
    """Распределение «змейкой» по посеву: 1→A, 2→B, …, затем в обратную сторону — группы равны по силе."""
    letters = LETTERS[:count]
    groups: dict[str, list[int]] = {g: [] for g in letters}
    for i, tid in enumerate(ordered_ids):
        row, col = divmod(i, count)
        groups[letters[col] if row % 2 == 0 else letters[count - 1 - col]].append(tid)
    return groups


def circle_rounds(ids: list[int]) -> list[list[tuple[int, int]]]:
    """Метод круга: один участник фиксирован, остальные вращаются. Нечётное число — выходной без матча."""
    arr: list[int | None] = list(ids)
    if len(arr) % 2:
        arr.append(None)
    n = len(arr)
    rounds: list[list[tuple[int, int]]] = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = arr[i], arr[n - 1 - i]
            if a is None or b is None:
                continue
            if r % 2 == 1 and i == 0:
                a, b = b, a  # чередование «первой» стороны у фиксированного участника
            pairs.append((a, b))
        rounds.append(pairs)
        arr = [arr[0], arr[-1]] + arr[1:-1]
    return rounds


class RoundRobinGroups(StageFormat):
    """Группы «все со всеми» (метод круга); из каждой группы выходят лучшие в плей-офф."""

    key = "groups_rr"
    title = "Группы (все со всеми) → плей-офф"
    min_teams = 4
    uses_slots = False
    allow_draw = True
    table = True
    standings_mode = "rr"
    default_groups = 2

    def normalize_settings(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        raw = dict(raw or {})
        bo = validate_bo(raw.get("bo_default", 1), allow_even=True)
        out = {
            "bo_default": bo, "bo_final": bo,
            "groups": _int_setting(raw, "groups", self.default_groups, 1, 16, "Число групп"),
            "advance": _int_setting(raw, "advance", 2, 1, 8, "Выходят из группы"),
        }
        out.update(_playoff_settings(raw, allow_none=self.key == "round_robin"))
        return out

    def feeds_playoffs(self, settings: dict[str, Any]) -> bool:
        return settings.get("playoff_format", "none") in PLAYOFF_FORMATS

    def bracket_title(self, key: str) -> str:
        return ("Группа " + key.split("-", 1)[1]) if key.startswith("group-") else key

    def build_table(self, ordered_ids, settings):
        count = int(settings["groups"])
        if len(ordered_ids) < 2 * count:
            raise ValueError(f"Для {count} групп нужно минимум {2 * count} команды.")
        groups = snake_groups(ordered_ids, count)
        smallest = min(len(v) for v in groups.values())
        if self.feeds_playoffs(settings) and int(settings["advance"]) >= smallest:
            raise ValueError(f"Из группы выходит {settings['advance']}, а в самой маленькой группе всего {smallest} команды.")
        if self.feeds_playoffs(settings) and int(settings["advance"]) * count < 2:
            raise ValueError("В плей-офф должно выйти минимум 2 команды.")
        specs: list[MatchSpec] = []
        meta: dict[int, dict[str, Any]] = {}
        for g, ids in groups.items():
            for tid in ids:
                meta[tid] = {"group": g}
            for r, pairs in enumerate(circle_rounds(ids), start=1):
                for i, (a, b) in enumerate(pairs, start=1):
                    specs.append(MatchSpec(key=f"{g}:R{r}-{i}", bracket=f"group-{g}", round_no=r, position=i,
                                           round_name=f"Тур {r}", best_of=settings["bo_default"], phase=r,
                                           src1=team(a), src2=team(b)))
        return specs, meta, {}

    def qualifiers(self, standings, settings):
        advance = int(settings.get("advance") or 0)
        out: list[int] = []
        for place in range(advance):  # сначала все победители групп, затем вторые места…
            for grp in standings:
                rows = grp["rows"]
                if place < len(rows):
                    out.append(int(rows[place]["team_id"]))
        return out

    def describe(self, settings):
        po = settings.get("playoff_format", "none")
        tail = (f" → плей-офф {'double' if po == 'double_elim' else 'single'} elimination (выходят {settings['advance']})"
                if po in PLAYOFF_FORMATS else "")
        return f"{self.title.split(' →')[0]} · групп {settings['groups']} · BO{settings['bo_default']}{tail}"


class RoundRobin(RoundRobinGroups):
    """Одна таблица «все со всеми»; победитель — первое место (или плей-офф, если выбран)."""

    key = "round_robin"
    title = "Круговая система (все со всеми)"
    min_teams = 3
    default_groups = 1

    def normalize_settings(self, raw):
        raw = dict(raw or {})
        raw["groups"] = 1
        return super().normalize_settings(raw)

    def bracket_title(self, key):
        return "Круговой турнир"

    def describe(self, settings):
        po = settings.get("playoff_format", "none")
        tail = f" → плей-офф (выходят {settings['advance']})" if po in PLAYOFF_FORMATS else ""
        return f"Круговая система · BO{settings['bo_default']}{tail}"


class GSLGroups(StageFormat):
    """Группы GSL на 4 команды: два стартовых матча, матч победителей, на вылет, решающий. Двое выходят."""

    key = "groups_gsl"
    title = "Группы GSL (до двух поражений) → плей-офф"
    min_teams = 3
    uses_slots = False
    table = True
    standings_mode = "gsl"

    def normalize_settings(self, raw):
        raw = dict(raw or {})
        out = {
            "bo_default": validate_bo(raw.get("bo_default", 1)),
            "groups": _int_setting(raw, "groups", 0, 0, 16, "Число групп"),  # 0 — автоматически по 4 команды
            "advance": 2,
        }
        out["bo_final"] = out["bo_default"]
        out.update(_playoff_settings(raw, allow_none=False))
        return out

    def feeds_playoffs(self, settings):
        return True

    def bracket_title(self, key):
        return ("Группа " + key.split("-", 1)[1]) if key.startswith("gsl-") else key

    def build_table(self, ordered_ids, settings):
        n = len(ordered_ids)
        count = int(settings.get("groups") or 0) or -(-n // 4)
        if not 3 * count <= n <= 4 * count:
            raise ValueError(f"В группе GSL 3–4 команды: для {count} групп нужно от {3 * count} до {4 * count} команд (сейчас {n}).")
        groups = snake_groups(ordered_ids, count)
        bo = settings["bo_default"]
        specs: list[MatchSpec] = []
        meta: dict[int, dict[str, Any]] = {}
        for g, ids in groups.items():
            s = list(ids) + [None] * (4 - len(ids))
            for tid in ids:
                meta[tid] = {"group": g}
            b = f"gsl-{g}"

            def spec(k, rnd, pos, name, s1, s2, phase):
                return MatchSpec(key=f"{g}:{k}", bracket=b, round_no=rnd, position=pos, round_name=name,
                                 best_of=bo, phase=phase, src1=s1, src2=s2)

            specs += [
                spec("O1", 1, 1, "Стартовые матчи", team(s[0]), team(s[3]), 1),
                spec("O2", 1, 2, "Стартовые матчи", team(s[1]), team(s[2]), 1),
                spec("W", 2, 1, "Матч победителей", winner(f"{g}:O1"), winner(f"{g}:O2"), 2),
                spec("E", 2, 2, "Матч на вылет", loser(f"{g}:O1"), loser(f"{g}:O2"), 2),
                spec("D", 3, 1, "Решающий матч", loser(f"{g}:W"), winner(f"{g}:E"), 3),
            ]
        return specs, meta, {"groups": count}

    def qualifiers(self, standings, settings):
        out: list[int] = []
        for place in range(2):
            for grp in standings:
                if place < len(grp["rows"]):
                    out.append(int(grp["rows"][place]["team_id"]))
        return out

    def describe(self, settings):
        po = settings.get("playoff_format")
        return f"Группы GSL · BO{settings['bo_default']} → плей-офф {'double' if po == 'double_elim' else 'single'} elimination"


class Swiss(StageFormat):
    """Швейцарка: пары внутри одинакового счёта без повторов; W побед — проход, L поражений — вылет.

    1-й тур: #1 против #(n/2+1)… (Valve); нечётное число — BYE (= победа) слабейшему без BYE.
    """

    key = "swiss"
    title = "Швейцарская система"
    min_teams = 4
    uses_slots = False
    dynamic_rounds = True
    table = True
    standings_mode = "swiss"

    def normalize_settings(self, raw):
        raw = dict(raw or {})
        out = {
            "bo_default": validate_bo(raw.get("bo_default", 1)),
            "wins_to_advance": _int_setting(raw, "wins_to_advance", 0, 0, 6, "Побед для выхода"),
            "losses_to_eliminate": _int_setting(raw, "losses_to_eliminate", 0, 0, 6, "Поражений для вылета"),
        }
        out["bo_final"] = out["bo_default"]
        out.update(_playoff_settings(raw, allow_none=True))
        return out

    @staticmethod
    def default_threshold(n: int) -> int:
        # 8 команд — 2 победы/2 поражения, 16 — 3/3 (как у Valve), 32 — 3/3.
        return (max(n, 2).bit_length() - 1) // 2 + 1

    def feeds_playoffs(self, settings):
        return settings.get("playoff_format", "none") in PLAYOFF_FORMATS

    def bracket_title(self, key):
        return "Швейцарская система"

    def build_table(self, ordered_ids, settings):
        n = len(ordered_ids)
        threshold = self.default_threshold(n)
        extra = {
            "wins_to_advance": int(settings.get("wins_to_advance") or threshold),
            "losses_to_eliminate": int(settings.get("losses_to_eliminate") or threshold),
            "swiss_byes": {},
        }
        ids = list(ordered_ids)
        if n % 2:
            extra["swiss_byes"][str(ids[-1])] = [1]
            ids = ids[:-1]
        half = len(ids) // 2
        specs = [MatchSpec(key=f"S1-{i + 1}", bracket="swiss", round_no=1, position=i + 1, round_name="Тур 1",
                           best_of=settings["bo_default"], phase=1, src1=team(ids[i]), src2=team(ids[i + half]))
                 for i in range(half)]
        return specs, {tid: {"group": None} for tid in ordered_ids}, extra

    def next_round(self, state):
        rows = {int(r["team_id"]): r for r in state["rows"]}
        wins_need, loss_need = int(state["wins_to_advance"]), int(state["losses_to_eliminate"])
        active = [r for r in rows.values() if r["wins"] < wins_need and r["losses"] < loss_need]
        if not active:
            return None
        if len(active) == 1:  # последней нерешённой команде не с кем играть — BYE (= победа)
            return [], [int(active[0]["team_id"])]
        active.sort(key=lambda r: (-r["wins"], r["losses"], -r.get("buchholz", 0), r.get("seed") or 999))
        byes = state.get("byes") or {}
        bye_team = None
        if len(active) % 2:
            for r in reversed(active):  # слабейший без BYE
                if not byes.get(str(r["team_id"])):
                    bye_team = r
                    break
            bye_team = bye_team or active[-1]
            active = [r for r in active if r is not bye_team]
        played: set[frozenset[int]] = state["played"]
        order = [int(r["team_id"]) for r in active]
        record = {int(r["team_id"]): (r["wins"], r["losses"]) for r in active}

        def pair(pool: list[int], allow_rematch: bool) -> list[tuple[int, int]] | None:
            if not pool:
                return []
            a, rest = pool[0], pool[1:]
            same = [b for b in rest if record[b] == record[a]]
            others = [b for b in rest if record[b] != record[a]]
            # внутри одинакового счёта — «верх против середины», затем ближайшие по счёту
            if same:
                k = len(same) // 2 if len(same) > 1 else 0
                same = same[k:] + same[:k]
            others.sort(key=lambda b: (abs(record[b][0] - record[a][0]) + abs(record[b][1] - record[a][1]), rest.index(b)))
            for b in same + others:
                if not allow_rematch and frozenset((a, b)) in played:
                    continue
                tail = pair([x for x in rest if x != b], allow_rematch)
                if tail is not None:
                    return [(a, b)] + tail
            return None

        pairs = pair(order, False) or pair(order, True) or []
        rnd = int(state["round"]) + 1
        specs = [MatchSpec(key=f"S{rnd}-{i + 1}", bracket="swiss", round_no=rnd, position=i + 1, round_name=f"Тур {rnd}",
                           best_of=int(state["bo"]), phase=rnd, src1=team(a), src2=team(b))
                 for i, (a, b) in enumerate(pairs)]
        return specs, ([int(bye_team["team_id"])] if bye_team else [])

    def qualifiers(self, standings, settings):
        rows = standings[0]["rows"] if standings else []
        return [int(r["team_id"]) for r in rows if r.get("status") == "advanced"]

    def describe(self, settings):
        po = settings.get("playoff_format", "none")
        tail = f" → плей-офф {'double' if po == 'double_elim' else 'single'} elimination" if po in PLAYOFF_FORMATS else ""
        return f"Швейцарская система · BO{settings['bo_default']}{tail}"


class ManualMatches(StageFormat):
    """Организатор сам выставляет пары, BO и время; таблица считается по сыгранным матчам."""

    key = "manual"
    title = "Ручные матчи (пары выставляет организатор)"
    min_teams = 2
    uses_slots = False
    allow_draw = True
    table = True
    standings_mode = "rr"

    def normalize_settings(self, raw):
        raw = dict(raw or {})
        bo = validate_bo(raw.get("bo_default", 1), allow_even=True)
        return {"bo_default": bo, "bo_final": bo, "playoff_format": "none", "advance": 0}

    def build_table(self, ordered_ids, settings):
        return [], {tid: {"group": "A"} for tid in ordered_ids}, {}

    def bracket_title(self, key):
        return "Общая таблица"

    def describe(self, settings):
        return "Ручные матчи · пары, BO и время выставляет организатор"


FORMATS: dict[str, StageFormat] = {f.key: f for f in (
    SingleElimination(), DoubleElimination(), RoundRobin(), RoundRobinGroups(), GSLGroups(), Swiss(), ManualMatches())}


def get_format(key: str) -> StageFormat:
    fmt = FORMATS.get(str(key or ""))
    if fmt is None:
        raise ValueError("Неизвестный формат турнира. Доступно: " + ", ".join(f.title for f in FORMATS.values()))
    return fmt


def formats_catalog() -> list[dict[str, Any]]:
    return [{"key": f.key, "title": f.title, "min_teams": f.min_teams} for f in FORMATS.values()]
