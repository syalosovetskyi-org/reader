"""Unit tests for backend.app.crypto — Fernet encrypt/decrypt round-trip. The key file
lives under the temp READER_SECRET_KEY_PATH set by conftest."""

from backend.app import crypto


def test_encrypt_decrypt_roundtrip():
    secret = "s3cret текст — with unicode"
    token = crypto.encrypt(secret)
    assert token != secret
    assert crypto.decrypt(token) == secret


def test_decrypt_empty_returns_empty():
    assert crypto.decrypt("") == ""


def test_encrypt_is_nondeterministic():
    # Fernet embeds a random IV + timestamp, so two encryptions differ but both decrypt
    a = crypto.encrypt("same")
    b = crypto.encrypt("same")
    assert a != b
    assert crypto.decrypt(a) == crypto.decrypt(b) == "same"


def test_decrypt_without_key_fails_loudly_and_does_not_mint_new_key(tmp_path, monkeypatch):
    # Ключ потерян (перенос каталога, другой READER_DATA_DIR), а в БД лежат
    # зашифрованные креды. Расшифровать их новым ключом нельзя в принципе, поэтому
    # decrypt обязан сказать «нет ключа», а не молча создать новый файл ключа и
    # упасть невнятным InvalidToken.
    key_path = tmp_path / "secret.key"
    monkeypatch.setattr(crypto, "SECRET_KEY_PATH", key_path)
    token = crypto.encrypt("password")
    key_path.unlink()

    try:
        crypto.decrypt(token)
    except Exception as exc:  # noqa: BLE001 — проверяем тип ниже
        err = exc
    else:
        raise AssertionError("decrypt без ключа обязан упасть")
    assert not key_path.exists(), "decrypt не должен создавать новый ключ"
    assert type(err).__name__ == "SecretKeyMissingError", repr(err)
    assert "secret.key" in str(err)


def test_new_key_is_created_with_0600_and_logged(tmp_path, monkeypatch, caplog):
    import logging
    import os
    import stat

    key_path = tmp_path / "sub" / "secret.key"
    monkeypatch.setattr(crypto, "SECRET_KEY_PATH", key_path)
    with caplog.at_level(logging.WARNING, logger="reader.crypto"):
        crypto.encrypt("x")
    assert key_path.exists()
    assert any("secret.key" in r.getMessage() for r in caplog.records)
    if os.name == "posix":
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
