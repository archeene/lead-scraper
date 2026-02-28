from pydantic import BaseModel


class Lead(BaseModel):
    id: str
    firstName: str
    lastName: str
    email: str | None = None
    phone: str | None = None
    status: str
    lastContactDate: str | None = None
    daysSinceContact: int | None = None
    source: str


class ScrapeResponse(BaseModel):
    leadCount: int
    leads: list[Lead]
    errors: list[str]
    metadata: dict
