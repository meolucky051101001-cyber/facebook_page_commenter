from cryptography.fernet import Fernet, InvalidToken
import config

_fernet = None


class TokenDecryptionError(ValueError):
    pass

def get_fernet():
    global _fernet
    if _fernet is None:
        key = config.ENCRYPTION_KEY
        if isinstance(key, str):
            key = key.encode("utf-8")
        _fernet = Fernet(key)
    return _fernet

def encrypt_token(raw_token: str) -> str:
    if not raw_token:
        return ""
    f = get_fernet()
    return f.encrypt(raw_token.encode("utf-8")).decode("utf-8")

def decrypt_token(enc_token: str) -> str:
    if not enc_token:
        return ""
    # In case token was stored unencrypted in legacy format, handle gracefully
    if enc_token.startswith("EAA"):
        return enc_token
    f = get_fernet()
    try:
        return f.decrypt(enc_token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError):
        raise TokenDecryptionError("Không giải mã được token Page. Kiểm tra khóa Fernet hoặc kết nối lại.") from None

def mask_token(token: str) -> str:
    """Returns a masked version of the token for logging/UI safety (e.g. '...4F7a9b')."""
    if not token or len(token) < 10:
        return "******"
    return f"...{token[-6:]}"
