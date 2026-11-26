"""Generate secrets: `python -m bot.keygen`.

Store the output in Render's environment AND in an offline backup (password
manager). If ENCRYPTION_KEYS or HMAC_KEY is lost, balances and user data in
Firestore cannot be read anymore.
"""
import secrets

from .security import generate_key

if __name__ == "__main__":
    print(f"ENCRYPTION_KEYS=v1:{generate_key()}")
    print(f"HMAC_KEY={generate_key()}")
    print(f"WEBHOOK_SECRET={secrets.token_urlsafe(32)}")
