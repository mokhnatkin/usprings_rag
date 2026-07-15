"""Хеширование и проверка паролей (argon2 через pwdlib).

Единая точка: логин, bootstrap super-admin и смена пароля идут через эти функции.
"""

from pwdlib import PasswordHash

# Рекомендованная конфигурация pwdlib - argon2.
_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    """Хеш пароля для хранения в БД."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Проверить пароль против хеша."""
    return _hasher.verify(password, password_hash)
