import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.main import app
from app.db.base import Base
from app.db.session import get_db
from app.core.security import hash_password
from app.models.user import User
from app.services.encryption_service import (
    encrypt_message,
    decrypt_message,
    _generate_conversation_key,
    _encrypt_key_with_master,
    _decrypt_key_with_master,
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///./test_encryption.db"


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
    for i, email in enumerate(["eve@test.com", "mallory@test.com"]):
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


class TestEncryptionService:

    def test_generate_conversation_key(self):
        key = _generate_conversation_key()
        assert len(key) == 32

    def test_encrypt_decrypt_key_with_master(self):
        conv_key = _generate_conversation_key()
        encrypted = _encrypt_key_with_master(conv_key)
        decrypted = _decrypt_key_with_master(encrypted)
        assert conv_key == decrypted
        assert encrypted != conv_key

    def test_encrypt_decrypt_message(self):
        key = _generate_conversation_key()
        plaintext = "Hello, this is a secret message!"
        body_b64, iv_b64 = encrypt_message(plaintext, key)
        decrypted = decrypt_message(body_b64, iv_b64, key)
        assert decrypted == plaintext
        assert body_b64 != plaintext

    def test_decrypt_wrong_key_fails(self):
        key1 = _generate_conversation_key()
        key2 = _generate_conversation_key()
        body_b64, iv_b64 = encrypt_message("secret", key1)
        with pytest.raises(Exception):
            decrypt_message(body_b64, iv_b64, key2)


class TestEncryptionIntegration:

    async def test_messages_encrypted_at_rest(self, client, two_users, db_session):
        headers = await auth_header(client, "eve@test.com")

        # Create conversation
        resp = await client.post("/api/v1/conversations", json={
            "user_id": two_users[1].id,
        }, headers=headers)
        assert resp.status_code == 200
        conv_id = resp.json()["id"]

        # Send a message via REST (simulating WebSocket - we test encryption by checking DB)
        from app.services.conversation_service import save_message
        await save_message(db_session, conv_id, two_users[0].id, "Hello encrypted world!")

        # Check that the body is encrypted in the DB
        from sqlalchemy import select
        from app.models.message import Message
        result = await db_session.execute(
            select(Message).where(Message.conversation_id == conv_id)
        )
        msg = result.scalar_one()
        assert msg.encrypted_body != "Hello encrypted world!"
        assert msg.iv != ""

        # Check that reading decrypts it
        resp = await client.get(f"/api/v1/conversations/{conv_id}/messages", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["messages"]) == 1
        assert data["messages"][0]["body"] == "Hello encrypted world!"

    async def test_list_conversations_decrypts_last_message(self, client, two_users, db_session):
        headers = await auth_header(client, "eve@test.com")

        resp = await client.post("/api/v1/conversations", json={
            "user_id": two_users[1].id,
        }, headers=headers)
        conv_id = resp.json()["id"]

        from app.services.conversation_service import save_message
        await save_message(db_session, conv_id, two_users[0].id, "Last message preview")

        resp = await client.get("/api/v1/conversations", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["last_message"] == "Last message preview"
