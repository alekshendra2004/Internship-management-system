"""Idempotency keys for create endpoints.

Clients send ``Idempotency-Key: <uuid>`` on POSTs that create records. The first request
with a key inserts a row in ``idempotency_keys`` *inside the same transaction* as the
record it creates, so:

* a retry after success replays the stored response (header ``Idempotent-Replayed: true``)
  and never creates a second record;
* two concurrent requests with the same key serialize on the unique index; the loser
  sees the stored response, or 409 while the first is still in flight;
* a request that fails rolls the key back with it, so the client may retry;
* reusing a key with a different payload is rejected (422).
"""
import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import field_error
from app.db.session import get_session
from app.models.idempotency import IdempotencyKey
from app.models.user import User

HEADER = "Idempotency-Key"
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")
RETENTION = timedelta(hours=24)


def _hash(payload: Any) -> str:
    if isinstance(payload, BaseModel):
        raw = payload.model_dump_json()
    else:
        raw = json.dumps(jsonable_encoder(payload), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class Idempotency:
    def __init__(self, session: AsyncSession, user: User, scope: str, key: str | None) -> None:
        self.session = session
        # Plain string: the ORM object may be expired after a rollback below.
        self.user_id = user.id
        self.scope = scope
        self.key = key
        self.row: IdempotencyKey | None = None

    async def replay(self, payload: Any) -> JSONResponse | None:
        """Return the stored response for a repeated key, or claim the key and return None."""
        if not self.key:
            return None
        if not KEY_PATTERN.match(self.key):
            raise field_error(HEADER, "Idempotency-Key must be 8-128 characters of [A-Za-z0-9_-:.]")
        request_hash = _hash(payload)
        existing = await self._find()
        if existing is None:
            self.row = IdempotencyKey(
                user_id=self.user_id,
                scope=self.scope,
                key=self.key,
                request_hash=request_hash,
                status="processing",
            )
            self.session.add(self.row)
            try:
                # Flush (not commit): the claim becomes durable only together with the
                # record created by this request.
                await self.session.flush()
                return None
            except IntegrityError:
                await self.session.rollback()
                self.row = None
                existing = await self._find()
                if existing is None:  # pragma: no cover - the competing request rolled back
                    raise HTTPException(status_code=409, detail="Concurrent request, please retry") from None
        return self._replay_existing(existing, request_hash)

    async def _find(self) -> IdempotencyKey | None:
        result = await self.session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == self.user_id,
                IdempotencyKey.scope == self.scope,
                IdempotencyKey.key == self.key,
            )
        )
        found = result.scalar_one_or_none()
        if found is not None:
            created = found.created_at if found.created_at.tzinfo else found.created_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - created > RETENTION:
                await self.session.delete(found)
                await self.session.flush()
                return None
        return found

    def _replay_existing(self, existing: IdempotencyKey, request_hash: str) -> JSONResponse:
        if existing.request_hash != request_hash:
            raise field_error(HEADER, "This Idempotency-Key was already used with a different request")
        if existing.status != "completed" or existing.response_status is None:
            raise HTTPException(
                status_code=409, detail="A request with this Idempotency-Key is still being processed"
            )
        return JSONResponse(
            status_code=existing.response_status,
            content=json.loads(existing.response_body or "null"),
            headers={"Idempotent-Replayed": "true"},
        )

    async def store(self, status_code: int, body: Any) -> JSONResponse:
        content = jsonable_encoder(body)
        if self.row is not None:
            # The service layer usually committed already; re-attach and finalise.
            row = await self.session.get(IdempotencyKey, self.row.id)
            if row is not None:
                row.status = "completed"
                row.response_status = status_code
                row.response_body = json.dumps(content)
                await self.session.commit()
        return JSONResponse(status_code=status_code, content=content)


def idempotent(scope: str) -> Callable[..., AsyncIterator[Idempotency]]:
    async def dependency(
        request: Request,
        user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> AsyncIterator[Idempotency]:
        yield Idempotency(session, user, scope, request.headers.get(HEADER))

    return dependency


async def purge_expired(session: AsyncSession) -> int:
    result = await session.execute(
        delete(IdempotencyKey).where(IdempotencyKey.created_at < datetime.now(UTC) - RETENTION)
    )
    await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)
