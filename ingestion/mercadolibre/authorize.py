"""One-time interactive authorization for the Mercado Libre app (owner only).

Run from the repo root (works on Windows with plain ``python``; needs only ``httpx``).

PowerShell:

    $env:ML_CLIENT_ID="..."
    $env:ML_CLIENT_SECRET="..."
    $env:ML_REDIRECT_URI="https://www.google.com"
    python -m ingestion.mercadolibre.authorize

cmd.exe (no quotes around the values):

    set ML_CLIENT_ID=...
    set ML_CLIENT_SECRET=...
    set ML_REDIRECT_URI=https://www.google.com
    python -m ingestion.mercadolibre.authorize

macOS/Linux: ``export ML_CLIENT_ID=...`` etc., then the same ``python -m`` command.

The app enforces PKCE (S256). This script generates a code_verifier/code_challenge, prints the
authorization URL, asks you to paste the ``code`` from the redirect, exchanges it, and prints the
refresh token ONCE so you can store it as the ``ML_REFRESH_TOKEN`` GitHub secret.
The access token is never printed.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlparse

from ingestion.mercadolibre.auth import AuthError, exchange_authorization_code

AUTHORIZATION_URL = "https://auth.mercadolibre.com.ar/authorization"
REQUIRED_ENV = ("ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_REDIRECT_URI")


def make_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` per RFC 7636 with method S256."""
    # 64 random bytes -> 86 URL-safe chars, within the 43..128 range required by RFC 7636.
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorization_url(client_id: str, redirect_uri: str, code_challenge: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZATION_URL}?{query}"


def extract_code(pasted: str) -> str:
    """Accept either the bare ``code`` or the whole redirect URL and return the code."""
    pasted = pasted.strip().strip('"').strip("'")
    if "://" in pasted or pasted.startswith("?") or "code=" in pasted:
        query = urlparse(pasted).query if "://" in pasted else pasted.lstrip("?")
        values = parse_qs(query).get("code")
        if not values or not values[0]:
            raise ValueError("No `code` parameter found in the pasted URL.")
        return values[0]
    if not pasted:
        raise ValueError("Nothing was pasted.")
    return pasted


def main() -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        print("Missing environment variables: " + ", ".join(missing), file=sys.stderr)
        print("Set them in this terminal session and run again.", file=sys.stderr)
        return 2

    client_id = os.environ["ML_CLIENT_ID"]
    client_secret = os.environ["ML_CLIENT_SECRET"]
    redirect_uri = os.environ["ML_REDIRECT_URI"]

    verifier, challenge = make_pkce_pair()
    print("Step 1. Open this URL in your browser, log in with the Mercado Libre account and")
    print("        grant access:")
    print()
    print("    " + build_authorization_url(client_id, redirect_uri, challenge))
    print()
    print(f"Step 2. You will be redirected to {redirect_uri}?code=...")
    print("        Copy the whole address bar (or only the code) and paste it here.")
    print("        The code is single-use and expires within minutes.")
    print()
    try:
        code = extract_code(input("Code or redirect URL: "))
    except (ValueError, EOFError) as exc:
        print(f"Could not read the code: {exc}", file=sys.stderr)
        return 2

    try:
        tokens = exchange_authorization_code(client_id, client_secret, code, redirect_uri, verifier)
    except AuthError as exc:
        print(
            f"Authorization failed: HTTP {exc.status} ({exc.error or 'unknown error'}).",
            file=sys.stderr,
        )
        print("Start again from Step 1 (codes are single-use and expire quickly).", file=sys.stderr)
        return 1

    print()
    print("Authorization succeeded.")
    print("Step 3. Store the refresh token below as the GitHub Actions secret ML_REFRESH_TOKEN")
    print("        (repo Settings > Secrets and variables > Actions). Do not save it in any file")
    print("        inside the repo. It rotates on every use; the daily job persists the new one.")
    print()
    print("ML_REFRESH_TOKEN=" + tokens.refresh_token)
    print()
    print("Clear this terminal (cls / clear) once you have stored it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
