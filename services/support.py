"""Поддержка: тикеты пользователей и ответы администраторов."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import SupportTicket, User
from database.repositories import SupportRepository, UserRepository

logger = logging.getLogger("SUPPORT")


class SupportService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.tickets = SupportRepository(session)
        self.users = UserRepository(session)

    async def create_ticket(self, user_id: int, message: str) -> SupportTicket:
        from utils.validation import validate_support_message

        ok, text, error = validate_support_message(message)
        if not ok:
            raise ValueError(error or "Пустое сообщение.")
        ticket = await self.tickets.create_ticket(user_id=user_id, message=text)
        await self.session.commit()
        return ticket

    async def get_for_admin(self, ticket_id: int) -> tuple[SupportTicket, User] | None:
        ticket = await self.tickets.get_by_id(ticket_id)
        if ticket is None:
            return None
        user = await self.users.get_by_id(ticket.user_id)
        if user is None:
            return None
        return ticket, user

    async def answer(self, ticket_id: int, reply: str, admin_telegram_id: int) -> SupportTicket:
        from utils.validation import validate_support_message

        ok, text, error = validate_support_message(reply)
        if not ok:
            raise ValueError(error or "Пустой ответ.")
        ticket = await self.tickets.get_by_id(ticket_id)
        if ticket is None:
            raise ValueError("Тикет не найден или уже закрыт.")
        ticket = await self.tickets.answer(ticket, text, admin_telegram_id)
        await self.session.commit()
        logger.info("[SUPPORT] Ticket %s answered by admin %s", ticket_id, admin_telegram_id)
        return ticket

    async def open_tickets(self, limit: int = 50) -> list[SupportTicket]:
        return await self.tickets.list_open(limit=limit)
