from config import Config
from database.models import Base
from miniapp.app import create_app


def _paths(app) -> set[str]:
    # FastAPI >= 0.13x хранит подключённые роутеры лениво (_IncludedRouter),
    # поэтому надёжный список путей — из OpenAPI-схемы.
    return set(app.openapi().get("paths", {}))


def test_miniapp_new_routes_registered():
    app = create_app(Config(bot_token="test"))
    routes = _paths(app)
    required = {
        "/api/chat/messages",
        "/api/chat/messages/{message_id}",
        "/api/events",
        "/api/notifications",
        "/api/notifications/read-all",
        "/api/notifications/{notification_id}/read",
        "/api/admin/admins",
        "/api/admin/admins/{telegram_id}",
        "/api/admin/ai/ask",
    }
    assert required <= routes


def test_miniapp_v2_routes_registered():
    app = create_app(Config(bot_token="test"))
    assert app.state.v2_mounted is True
    routes = _paths(app)
    required = {
        "/api/v2/me", "/api/v2/profile", "/api/v2/profile/steam", "/api/v2/team", "/api/v2/team/leave",
        "/api/v2/players", "/api/v2/teams", "/api/v2/tournaments", "/api/v2/chat", "/api/v2/support",
        "/api/v2/admin/stats", "/api/v2/admin/users", "/api/v2/admin/teams", "/api/v2/admin/tournaments",
        "/api/v2/admin/applications", "/api/v2/admin/support", "/api/v2/admin/diagnostics", "/api/v2/admin/avatars/sync",
    }
    assert required <= routes


def test_activity_admin_tables_exist_in_metadata():
    required = {"admin_users", "chat_messages", "activity_events", "notifications"}
    assert required <= set(Base.metadata.tables)
