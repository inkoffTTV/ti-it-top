"""Совместимость: handlers.registration -> handlers.profile.

Ранняя версия проекта импортировала ``begin_registration`` из модуля
``handlers.registration``, которого фактически не существовало — это приводило
к ``ModuleNotFoundError`` при нажатии кнопки «▶️ Начать регистрация».

Логика регистрации живёт в :mod:`handlers.profile`. Этот файл — тонкий
алиас, чтобы любой старый импорт (и ваш локальный код до обновления start.py)
продолжал работать без изменений.
"""

from handlers.profile import (  # noqa: F401
    RegistrationStates,
    begin_registration,
)

__all__ = ["RegistrationStates", "begin_registration"]
