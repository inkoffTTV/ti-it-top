"""Общие зависимости и FSM-состояния для всех роутеров."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup

from config import Config, GLOBAL_CONFIG, is_admin


def get_config() -> Config:
    return GLOBAL_CONFIG


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class RegistrationStates(StatesGroup):
    waiting_steam = State()
    waiting_mmr = State()


class TeamStates(StatesGroup):
    waiting_name = State()
    waiting_add_player = State()
    waiting_remove_player = State()
    waiting_avatar = State()
    waiting_invite = State()


class TournamentCreateStates(StatesGroup):
    waiting_name = State()
    waiting_description = State()
    waiting_max_teams = State()
    waiting_start_date = State()


class SupportStates(StatesGroup):
    waiting_message = State()


class AdminReplyStates(StatesGroup):
    waiting_reply = State()          # ответ в тикет поддержки
    waiting_comment = State()        # комментарий админа к заявке/проверке


# ---------------------------------------------------------------------------
# Callback data форматы (справочно):
#   nav:home | nav:tournaments
#   action:cancel
#   reg:start
#   team:create|menu|roster|status|add|remove ; team:kick:<user_db_id>
#   trn:view:<id> | trn:join:<id>
#   app:approve:<id> | app:reject:<id> | app:comment:<id>
#   ver:approve:<profile_id> | ver:reject:<profile_id> | ver:recheck:<profile_id>
#   adm:menu|users|teams|tournaments|applications|verification|support|stats
#   adm:user:<user_db_id> | adm:usearch | adm:urecheck:<user_db_id>
#   adm:tcreate | adm:tournament:<id> | trn:open:<id>|close|start|finish|cancel
#   sup:ticket:<id> | sup:reply:<id>
# ---------------------------------------------------------------------------


def require_admin(user_id: int | None, config: Config | None = None) -> bool:
    return is_admin(user_id, config or get_config())
