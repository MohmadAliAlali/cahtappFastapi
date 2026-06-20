import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.main import app
from app.db.base import Base
from app.db.session import get_db
from app.core.security import hash_password
from app.models.user import User

TEST_DATABASE_URL = "sqlite+aiosqlite:///./test.db"


@pytest.fixture
async def db_session():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def client(db_session):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def two_users(db_session):
    users = []
    for i, email in enumerate(["alice@test.com", "bob@test.com"]):
        u = User(
            email=email,
            hashed_password=hash_password("pass123"),
            display_name=f"User{i}",
            is_active=True,
        )
        db_session.add(u)
        users.append(u)
    await db_session.commit()
    for u in users:
        await db_session.refresh(u)
    return users


async def auth_header(client, email):
    resp = await client.post("/api/v1/auth/login", json={
        "email": email, "password": "pass123",
    })
    assert resp.status_code == 200
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


async def test_create_conversation(client, two_users):
    headers = await auth_header(client, "alice@test.com")
    resp = await client.post("/api/v1/conversations", json={
        "user_id": two_users[1].id,
    }, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["type"] == "direct"


async def test_create_conversation_self(client, two_users):
    headers = await auth_header(client, "alice@test.com")
    resp = await client.post("/api/v1/conversations", json={
        "user_id": two_users[0].id,
    }, headers=headers)
    assert resp.status_code == 400


async def test_create_conversation_not_found(client, two_users):
    headers = await auth_header(client, "alice@test.com")
    resp = await client.post("/api/v1/conversations", json={
        "user_id": "nonexistent",
    }, headers=headers)
    assert resp.status_code == 404


async def test_create_conversation_idempotent(client, two_users):
    headers = await auth_header(client, "alice@test.com")
    resp1 = await client.post("/api/v1/conversations", json={
        "user_id": two_users[1].id,
    }, headers=headers)
    resp2 = await client.post("/api/v1/conversations", json={
        "user_id": two_users[1].id,
    }, headers=headers)
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["id"] == resp2.json()["id"]


async def test_list_conversations_empty(client, two_users):
    headers = await auth_header(client, "alice@test.com")
    resp = await client.get("/api/v1/conversations", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_conversations_after_create(client, two_users):
    headers = await auth_header(client, "alice@test.com")
    await client.post("/api/v1/conversations", json={
        "user_id": two_users[1].id,
    }, headers=headers)
    resp = await client.get("/api/v1/conversations", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["type"] == "direct"


async def test_get_messages_no_auth(client, two_users):
    resp = await client.get("/api/v1/conversations/some-id/messages")
    assert resp.status_code in (401, 403)


async def test_mark_read_no_conversation(client, two_users):
    headers = await auth_header(client, "alice@test.com")
    resp = await client.post("/api/v1/conversations/nonexistent/read", json={
        "last_read_msg_id": "some-msg-id",
    }, headers=headers)
    assert resp.status_code == 403
