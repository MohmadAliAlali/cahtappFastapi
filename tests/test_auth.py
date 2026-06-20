import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.main import app
from app.db.base import Base
from app.db.session import get_db

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


async def test_register(client):
    response = await client.post("/api/v1/auth/register", json={
        "email": "test@example.com",
        "password": "password123",
        "display_name": "Test User",
    })
    assert response.status_code == 200
    data = response.json()
    assert "message" in data


async def test_register_duplicate(client):
    await client.post("/api/v1/auth/register", json={
        "email": "dupe@example.com",
        "password": "password123",
        "display_name": "Dupe",
    })
    response = await client.post("/api/v1/auth/register", json={
        "email": "dupe@example.com",
        "password": "password123",
        "display_name": "Dupe",
    })
    assert response.status_code == 409


async def test_login_before_verify(client):
    await client.post("/api/v1/auth/register", json={
        "email": "unverified@example.com",
        "password": "password123",
        "display_name": "Unverified",
    })
    response = await client.post("/api/v1/auth/login", json={
        "email": "unverified@example.com",
        "password": "password123",
    })
    assert response.status_code == 403


async def test_login_invalid_credentials(client):
    response = await client.post("/api/v1/auth/login", json={
        "email": "nonexistent@example.com",
        "password": "wrong",
    })
    assert response.status_code == 401


async def test_resend_code(client):
    await client.post("/api/v1/auth/register", json={
        "email": "resend@example.com",
        "password": "password123",
        "display_name": "Resend",
    })
    response = await client.post("/api/v1/auth/resend-code", json={
        "email": "resend@example.com",
    })
    assert response.status_code == 200


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["database"] == "ok"
    assert data["redis"] in ("ok", "disabled")


async def test_full_auth_flow(client):
    reg_resp = await client.post("/api/v1/auth/register", json={
        "email": "fullflow@example.com",
        "password": "password123",
        "display_name": "Full Flow",
    })
    assert reg_resp.status_code == 200

    login_resp = await client.post("/api/v1/auth/login", json={
        "email": "fullflow@example.com",
        "password": "password123",
    })
    assert login_resp.status_code == 403

    bad_verify = await client.post("/api/v1/auth/verify-email", json={"code": "000000"})
    assert bad_verify.status_code == 400
