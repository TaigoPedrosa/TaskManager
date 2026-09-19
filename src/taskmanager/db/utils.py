from datetime import UTC, datetime


def to_db_timestamp(val: datetime | None = None) -> str:
    if val is None:
        return datetime.now(tz=UTC).isoformat()
    if val.tzinfo is None:
        return val.astimezone(UTC).isoformat()
    return val.astimezone(UTC).isoformat()


def parse_db_datetime(val: str | datetime) -> datetime:
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.astimezone(UTC)
        return val.astimezone(UTC)
    dt = datetime.fromisoformat(val.strip())
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
