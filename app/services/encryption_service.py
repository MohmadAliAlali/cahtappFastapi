import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.encryption import EncryptionKey

_MASTER_KEY_V2: bytes | None = None  # HKDF-derived (new)
_MASTER_KEY_V1: bytes | None = None  # Old padded SECRET_KEY (legacy)


def _get_master_key_v2() -> bytes:
    """New HKDF-derived master key (v2)."""
    global _MASTER_KEY_V2
    if _MASTER_KEY_V2 is None:
        salt = b"chat-app-master-key-salt-v1"
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=b"chat-app-encryption-master-key",
        )
        _MASTER_KEY_V2 = hkdf.derive(settings.SECRET_KEY.encode())
    return _MASTER_KEY_V2


def _get_master_key_v1() -> bytes:
    """Legacy padded SECRET_KEY master key (v1) for backward compatibility."""
    global _MASTER_KEY_V1
    if _MASTER_KEY_V1 is None:
        raw = settings.SECRET_KEY.encode()
        if len(raw) < 32:
            raw = raw.ljust(32, b'\0')
        _MASTER_KEY_V1 = raw[:32]
    return _MASTER_KEY_V1


def _get_master_key() -> bytes:
    """Default to new v2 key."""
    return _get_master_key_v2()


def _generate_conversation_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def _encrypt_key_with_master(conversation_key: bytes) -> str:
    master = _get_master_key()
    aesgcm = AESGCM(master)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, conversation_key, None)
    return base64.b64encode(nonce + ciphertext).decode()


def _decrypt_key_with_master(encrypted_key_b64: str) -> bytes:
    """Try v2 (HKDF) first, fall back to v1 (legacy padded) for old keys."""
    data = base64.b64decode(encrypted_key_b64)
    nonce = data[:12]
    ciphertext = data[12:]

    # Try new v2 key first
    try:
        aesgcm_v2 = AESGCM(_get_master_key_v2())
        return aesgcm_v2.decrypt(nonce, ciphertext, None)
    except InvalidTag:
        pass

    # Fall back to legacy v1 key
    try:
        aesgcm_v1 = AESGCM(_get_master_key_v1())
        return aesgcm_v1.decrypt(nonce, ciphertext, None)
    except InvalidTag:
        pass

    raise InvalidTag("Unable to decrypt key with either v2 or v1 master key")


def encrypt_message(plaintext: str, conversation_key: bytes) -> tuple[str, str]:
    aesgcm = AESGCM(conversation_key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    iv_b64 = base64.b64encode(nonce).decode()
    body_b64 = base64.b64encode(ciphertext).decode()
    return body_b64, iv_b64


def decrypt_message(encrypted_body_b64: str, iv_b64: str, conversation_key: bytes) -> str:
    aesgcm = AESGCM(conversation_key)
    nonce = base64.b64decode(iv_b64)
    ciphertext = base64.b64decode(encrypted_body_b64)
    return aesgcm.decrypt(nonce, ciphertext, None).decode()


async def get_or_create_conversation_key(
    db: AsyncSession, conversation_id: str
) -> bytes:
    result = await db.execute(
        select(EncryptionKey).where(EncryptionKey.conversation_id == conversation_id)
    )
    key_record = result.scalar_one_or_none()

    if key_record:
        return _decrypt_key_with_master(key_record.encrypted_key)

    conv_key = _generate_conversation_key()
    encrypted_key = _encrypt_key_with_master(conv_key)

    key_record = EncryptionKey(
        conversation_id=conversation_id,
        encrypted_key=encrypted_key,
    )
    db.add(key_record)
    await db.flush()
    return conv_key