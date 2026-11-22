from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.security import CurrentUser, get_current_user

router = APIRouter(tags=["auth"])


@router.get("/me")
def me(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    """Get the current authenticated user."""
    return user
