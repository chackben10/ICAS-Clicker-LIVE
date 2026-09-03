#!/usr/bin/env python3
"""Create and verify the persistent local code-signing identity.

macOS ties a privacy grant (Accessibility, Input Monitoring, Camera, local
network) to an application's Designated Requirement.  An ad-hoc signature ("-")
produces a requirement that names one exact binary, so every rebuild looks like
a different application and every grant has to be given again -- while the
toggle in System Settings still reads "On".

A self-signed certificate in the login keychain fixes that: the requirement
names the certificate rather than the bytes, so rebuilds keep their grants.

Creating the identity touches the login keychain, so this script never does it
silently.  Run it directly, once:

    python3 Production_Hub_App/scripts/ensure_codesign_identity.py --create

Afterwards every build picks the identity up automatically.

Two macOS details this script cannot do for you, both of which have already
caused a silent multi-minute stall once:

* `security add-trusted-cert` (below) prompts for the login password.
* The first `codesign` run that uses the key prompts again -- "codesign wants to
  access key ... in your keychain" -- and blocks the build until answered.
  Choose ALWAYS ALLOW; plain "Allow" returns the prompt on every rebuild.
  `-T /usr/bin/codesign` at import time is not sufficient on modern macOS; the
  key's partition list has to be set as well, which needs the password on the
  command line and so is left to the operator.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path


IDENTITY_NAME = "Production Hub Local Signing"
LOGIN_KEYCHAIN = "login.keychain-db"


class IdentityError(RuntimeError):
    pass


def _run(
    command: list[str],
    *,
    check: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = None
    if environment is not None:
        merged = os.environ.copy()
        merged.update(environment)
    process = subprocess.run(command, text=True, capture_output=True, env=merged)
    if check and process.returncode != 0:
        raise IdentityError(
            f"Command failed: {' '.join(command)}\n{(process.stderr or process.stdout).strip()}"
        )
    return process


def find_identity(name: str = IDENTITY_NAME) -> str:
    """Return the SHA-1 hash of the named code-signing identity, or ""."""

    process = _run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"], check=False)
    for line in process.stdout.splitlines():
        if f'"{name}"' not in line:
            continue
        match = re.search(r"\b([0-9A-F]{40})\b", line)
        if match:
            return match.group(1)
    return ""


def signing_identity(name: str = IDENTITY_NAME) -> str:
    """Return the identity string to hand to codesign, or "-" when absent."""

    return name if find_identity(name) else "-"


def create_identity(name: str = IDENTITY_NAME) -> str:
    """Create a self-signed code-signing certificate in the login keychain."""

    if find_identity(name):
        return name

    keychain = Path.home() / "Library" / "Keychains" / LOGIN_KEYCHAIN
    if not keychain.is_file():
        raise IdentityError(f"Login keychain not found at {keychain}")

    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        key_path = workspace / "identity.key"
        certificate_path = workspace / "identity.crt"
        bundle_path = workspace / "identity.p12"
        # codeSigning EKU plus a CA basic constraint: the certificate signs the
        # app and is its own issuer, which is what a self-signed identity is.
        _run(
            [
                "/usr/bin/openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "3650",
                "-subj",
                f"/CN={name}",
                # No -extensions/-config: pulling in the system openssl.cnf's
                # v3_req section as well as -addext emits each extension twice
                # (CA:FALSE and CA:TRUE, two Key Usages). Duplicate X509v3
                # extensions are malformed under RFC 5280 and macOS may reject
                # the certificate. -addext alone produces one clean set.
                "-keyout",
                str(key_path),
                "-out",
                str(certificate_path),
                "-addext",
                "basicConstraints=critical,CA:true",
                "-addext",
                "keyUsage=critical,digitalSignature",
                "-addext",
                "extendedKeyUsage=critical,codeSigning",
            ]
        )
        # A throwaway transport password. macOS's `security import` cannot read
        # a PKCS#12 bundle exported with an empty password -- it fails with
        # "MAC verification failed during PKCS12 import" -- so the bundle gets a
        # random one. It exists only to carry the key from openssl into the
        # keychain and is discarded with the temporary directory.
        transport_password = secrets.token_urlsafe(24)
        _run(
            [
                "/usr/bin/openssl",
                "pkcs12",
                "-export",
                "-inkey",
                str(key_path),
                "-in",
                str(certificate_path),
                "-out",
                str(bundle_path),
                "-passout",
                "env:PH_IDENTITY_PASSWORD",
                "-name",
                name,
            ],
            environment={"PH_IDENTITY_PASSWORD": transport_password},
        )
        # -T trusts codesign to use the key without prompting on every build.
        _run(
            [
                "/usr/bin/security",
                "import",
                str(bundle_path),
                "-k",
                str(keychain),
                "-P",
                transport_password,
                "-T",
                "/usr/bin/codesign",
                "-T",
                "/usr/bin/productsign",
            ]
        )
        _run(
            [
                "/usr/bin/security",
                "add-trusted-cert",
                "-r",
                "trustRoot",
                "-p",
                "codeSign",
                "-k",
                str(keychain),
                str(certificate_path),
            ]
        )

    if not find_identity(name):
        raise IdentityError(
            f"{name!r} was imported but is not listed as a code-signing identity. "
            "Open Keychain Access and confirm the certificate is trusted for code signing."
        )
    return name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default=IDENTITY_NAME, help=f"Identity common name. Defaults to {IDENTITY_NAME!r}.")
    parser.add_argument(
        "--create",
        action="store_true",
        help="Create the identity when it is missing. Without this the script only reports.",
    )
    arguments = parser.parse_args()

    existing = find_identity(arguments.name)
    if existing:
        print(f"Code-signing identity present: {arguments.name} ({existing})")
        return 0
    if not arguments.create:
        print(
            f"Code-signing identity {arguments.name!r} is missing; builds will fall back to ad-hoc\n"
            "signing and macOS will drop privacy permissions on every rebuild.\n"
            "Create it with:\n\n"
            f"    python3 {Path(__file__).resolve()} --create\n",
            file=sys.stderr,
        )
        return 1
    try:
        created = create_identity(arguments.name)
    except IdentityError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Created code-signing identity: {created} ({find_identity(created)})")
    print()
    print("The FIRST build using this identity will stop with a keychain dialog:")
    print('  "codesign wants to access key \"identity\" in your keychain."')
    print("Enter the login password and click ALWAYS ALLOW -- plain \"Allow\" grants")
    print("it once and the prompt returns on every rebuild. Until it is answered the")
    print("build simply waits, with no output.")
    print()
    print("To pre-authorise it instead and avoid the dialog entirely, run:")
    print("  security set-key-partition-list -S apple-tool:,apple:,codesign: \\")
    print(f"    -s -k <login-password> {Path.home() / 'Library' / 'Keychains' / LOGIN_KEYCHAIN}")
    print()
    print("Rebuild and reinstall once; permissions granted after that survive later rebuilds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
