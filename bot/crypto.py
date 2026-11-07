from cryptography.fernet import Fernet, InvalidToken

from .config import ENCRYPTION_KEY

try:
    _fernet = Fernet(ENCRYPTION_KEY.encode())
except Exception as exc:  # bad key format
    raise SystemExit(
        "[config] ENCRYPTION_KEY is invalid. Generate one with:\n"
        '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    ) from exc


def encrypt(text: str) -> str:
    return _fernet.encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Stored session cannot be decrypted (ENCRYPTION_KEY changed?)") from exc
