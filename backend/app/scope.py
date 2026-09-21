"""Who may see which rows.

ONE helper answers that question for the whole API: `current_scope`. Every
data endpoint depends on it, and every query is filtered through the `Scope`
it returns. There is deliberately no second place where a role is interpreted
-- a rule implemented twice is a rule that will be changed once.

The role is read from the database on every request. It is never taken from
the token, because a JWT is issued by Supabase Auth and its claims are
whatever the signup flow put there; a user who can edit their own metadata
could otherwise promote themselves by editing a claim. The only thing the
token is trusted for is *identity* (the `sub`), which is what it is signed
for.

The `codes` field has three states and they are not interchangeable:

    codes = None   ceo. Unrestricted. INCLUDES rows with a null
                   salesperson_code -- the 30 line items worth 33,503.50 that
                   no salesperson owns. This is why `codes = None` cannot be
                   replaced by "a list of every code": that list would still
                   drop the nulls, and the ceo total would come to
                   44,459,976.39 instead of 44,493,479.89.
    codes = [...]  sales or sales_manager. Exactly these codes, nulls excluded.
    codes = []     a signed-in user with no profile, or a manager with an
                   empty team. Sees nothing. Fails closed, on purpose.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException

from . import db
from .auth import User, current_user

ROLE_SALES = "sales"
ROLE_MANAGER = "sales_manager"
ROLE_CEO = "ceo"
ROLE_NONE = "none"
ROLES = (ROLE_SALES, ROLE_MANAGER, ROLE_CEO)

# Which roles may open which page. Absent from this map == open to any role.
PAGE_ROLES = {
    "upload": {ROLE_CEO},
    "accuracy": {ROLE_MANAGER, ROLE_CEO},
    "admin": {ROLE_CEO},
}

PAGE_TH = {
    "upload": "อัปโหลดข้อมูล",
    "accuracy": "ความแม่นยำพยากรณ์",
    "admin": "ผู้ใช้และสิทธิ์",
    "forecast": "พยากรณ์",
    "stock": "สินค้าคงคลัง",
}


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def sales_can_see(page: str) -> bool:
    """Config flags for the two pages the owner has not decided about.

    Default OFF for both, per the brief. They are env flags rather than rows
    in a table because the decision applies to the role, not to a person, and
    because flipping it must not require a migration on the day the owner
    makes up their mind.
    """
    return {
        "forecast": _flag("SALES_CAN_SEE_FORECAST", False),
        "stock": _flag("SALES_CAN_SEE_STOCK", False),
    }.get(page, True)


@dataclass
class Scope:
    user_id: str
    email: str | None
    role: str
    codes: list[str] | None = None
    team: list[str] = field(default_factory=list)
    own_code: str | None = None

    @property
    def unrestricted(self) -> bool:
        """True only for the ceo: no row filter at all, nulls included."""
        return self.codes is None

    def may_open(self, page: str) -> bool:
        allowed = PAGE_ROLES.get(page)
        if allowed is not None and self.role not in allowed:
            return False
        if self.role == ROLE_NONE:
            return False
        if self.role == ROLE_SALES and not sales_can_see(page):
            return False
        return True

    def require(self, page: str) -> None:
        if self.may_open(page):
            return
        name = PAGE_TH.get(page, page)
        raise HTTPException(403, f"ไม่มีสิทธิ์เข้าถึงหน้า{name} (บทบาท: {self.role})")

    def as_json(self) -> dict:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "role": self.role,
            "own_code": self.own_code,
            "team": self.team,
            "codes": self.codes,
            "unrestricted": self.unrestricted,
            "pages": {p: self.may_open(p) for p in
                      ("overview", "sales", "demand", "forecast", "stock",
                       "accuracy", "customers", "upload", "admin")},
        }


def _dev_scope(user: User) -> Scope:
    """CSV-fallback mode only: there is no database to read a role from.

    Production always has DATABASE_URL, so this branch never runs there. It
    defaults to `none` rather than `ceo` so that a misconfigured deployment
    that lost its DATABASE_URL serves nothing instead of serving everything.
    """
    role = os.environ.get("DEV_ROLE", "").strip().lower() or ROLE_NONE
    if role not in ROLES:
        role = ROLE_NONE
    codes_raw = [c.strip() for c in os.environ.get("DEV_CODES", "").split(",") if c.strip()]
    if role == ROLE_CEO:
        return Scope(user.id, user.email, ROLE_CEO, None)
    return Scope(user.id, user.email, role, codes_raw,
                 team=codes_raw if role == ROLE_MANAGER else [],
                 own_code=codes_raw[0] if (role == ROLE_SALES and codes_raw) else None)


def load_scope(user: User) -> Scope:
    """The one place a role becomes a row filter."""
    if not os.environ.get("DATABASE_URL", "").strip():
        return _dev_scope(user)

    if not user.id or user.id == "anonymous":
        return Scope(user.id or "anonymous", user.email, ROLE_NONE, [])

    try:
        rows = db.fetch(
            "select role, salesperson_code from user_profiles where user_id = %s",
            (user.id,),
        )
    except Exception as exc:
        # Fail closed. An unreadable profile table must not mean "no filter".
        raise HTTPException(503, f"อ่านสิทธิ์ผู้ใช้ไม่สำเร็จ: {exc}")

    if not rows:
        return Scope(user.id, user.email, ROLE_NONE, [])

    role = (rows[0]["role"] or "").strip()
    own = (rows[0]["salesperson_code"] or "").strip() or None
    if role not in ROLES:
        return Scope(user.id, user.email, ROLE_NONE, [])

    if role == ROLE_CEO:
        return Scope(user.id, user.email, ROLE_CEO, None, own_code=own)

    if role == ROLE_MANAGER:
        team = [r["salesperson_code"] for r in db.fetch(
            "select distinct salesperson_code from team_members "
            "where manager_user_id = %s order by salesperson_code", (user.id,))]
        return Scope(user.id, user.email, ROLE_MANAGER, list(team), team=list(team))

    return Scope(user.id, user.email, ROLE_SALES,
                 [own] if own else [], own_code=own)


async def current_scope(user: User = Depends(current_user)) -> Scope:
    return load_scope(user)


def require_ceo(scope: Scope = Depends(current_scope)) -> Scope:
    scope.require("admin")
    return scope
