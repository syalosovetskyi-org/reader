"""Шифрование секретов аккаунтов (Fernet). Ключ — в файле вне репо (SECRET_KEY_PATH),
создаётся при первом ШИФРОВАНИИ, сразу с правами 600.

Расшифровка ключ не создаёт никогда: токен, зашифрованный прежним ключом, новым
не открыть, поэтому потерянный ключ (перенос каталога, другой READER_DATA_DIR)
должен быть виден как «нет ключа», а не как молча созданный новый файл и
невнятный InvalidToken на каждом аккаунте (serg/tasks#3045)."""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet

from .config import SECRET_KEY_PATH

_log = logging.getLogger("reader.crypto")


class SecretKeyMissingError(RuntimeError):
    """Файла ключа нет, а расшифровать надо — данные зашифрованы другим ключом."""


def _create_key() -> bytes:
    SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # O_EXCL: файл создаётся сразу с 0600 (без окна с правами по umask) и не
    # перетирает ключ, который параллельно успел создать другой процесс.
    try:
        fd = os.open(
            SECRET_KEY_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        return SECRET_KEY_PATH.read_bytes()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    _log.warning(
        "создан НОВЫЙ ключ шифрования %s — секреты, зашифрованные прежним ключом, "
        "этим не расшифровать",
        SECRET_KEY_PATH,
    )
    return key


def _key(*, create: bool) -> bytes:
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_bytes()
    if not create:
        raise SecretKeyMissingError(
            f"нет файла ключа шифрования {SECRET_KEY_PATH}: сохранённые креды и куки "
            "зашифрованы ключом, которого здесь нет (перенесите secret.key или "
            "проверьте READER_SECRET_KEY_PATH / READER_DATA_DIR)"
        )
    return _create_key()


def encrypt(plaintext: str) -> str:
    return Fernet(_key(create=True)).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    return Fernet(_key(create=False)).decrypt(token.encode("ascii")).decode("utf-8")
