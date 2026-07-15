"""Хеширование паролей (argon2) - round-trip, без БД."""

from usprings_rag.security import hash_password, verify_password


def test_verify_accepts_correct_and_rejects_wrong():
    h = hash_password("correct horse")
    assert verify_password("correct horse", h)
    assert not verify_password("wrong", h)


def test_hashes_are_salted_and_unique():
    # Соль случайна: один пароль даёт разные хеши, оба валидны.
    a = hash_password("pass")
    b = hash_password("pass")
    assert a != b
    assert verify_password("pass", a)
    assert verify_password("pass", b)
