from __future__ import annotations

from pathlib import Path


class CredentialStore:
    """Small credential abstraction ready for keychain integration.

    The first version keeps credentials in the validated profile so migration
    from the current plaintext PTZ script is explicit. This wrapper prevents
    service code from reaching into config files directly.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def describe_backend(self) -> str:
        return "profile-backed; macOS Keychain adapter pending"

