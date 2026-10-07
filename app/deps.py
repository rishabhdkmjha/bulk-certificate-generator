from __future__ import annotations

import secrets
from typing import Iterator

from fastapi import Header, HTTPException, Request
from sqlalchemy.orm import Session


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as db:
        yield db


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    expected = request.app.state.settings.api_key
    # Compare as bytes: secrets.compare_digest() raises TypeError for non-ASCII *str*,
    # which would turn a bad header into a 500 instead of a 401.
    if expected and not secrets.compare_digest((x_api_key or "").encode("utf-8"),
                                               expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")