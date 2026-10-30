"""Player registration flow (Steam identity -> verification -> profile).

Реализация находится в handlers/profile.py; этот модуль ре-экспортирует её,
чтобы работали оба стиля импорта:

    from handlers.registration import begin_registration, RegistrationStates
"""

from handlers.profile import RegistrationStates, begin_registration

__all__ = ["RegistrationStates", "begin_registration"]