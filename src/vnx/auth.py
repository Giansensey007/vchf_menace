from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed


def sort_object_deep(value: Any) -> Any:
    if isinstance(value, list):
        return [sort_object_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: sort_object_deep(value[k]) for k in sorted(value.keys())}
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def canonical_vnx_body(payload: dict[str, Any]) -> str:
    """JSON body matching Node JSON.stringify(sortObjectDeep(payload))."""
    return json.dumps(sort_object_deep(payload), separators=(",", ":"))


def load_private_key_pem() -> str:
    b64 = os.getenv("VNX_PRIVATE_KEY_B64", "").strip()
    if not b64:
        raise ValueError("VNX_PRIVATE_KEY_B64 not set")
    return base64.b64decode(b64).decode("utf-8")


def derive_public_key_b64() -> str:
    """Derive SPKI public key (base64) from PEM private key."""
    pem = load_private_key_pem()
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("VNX private key must be ECDSA")
    pub_bytes = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(pub_bytes).decode("ascii")


def ensure_public_key_env() -> str:
    pub = os.getenv("VNX_API_PUBLIC_KEY", "").strip()
    if pub:
        return pub
    pub = derive_public_key_b64()
    os.environ["VNX_API_PUBLIC_KEY"] = pub
    return pub


def sign_request(path: str, payload: dict[str, Any], nonce: int | None = None) -> tuple[str, int, str]:
    """Return (public_key_b64, nonce, signature_b64)."""
    public_key = ensure_public_key_env()
    pem = load_private_key_pem()
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("VNX private key must be ECDSA")

    if nonce is None:
        nonce = int(time.time() * 1000)

    sorted_payload = sort_object_deep(payload)
    body = canonical_vnx_body(payload)
    data = path + body + str(nonce)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data.encode("utf-8"))
    hashed = digest.finalize()
    signature = key.sign(hashed, ec.ECDSA(Prehashed(hashes.SHA256())))
    return public_key, nonce, base64.b64encode(signature).decode("ascii")


def auth_headers(path: str, payload: dict[str, Any], nonce: int | None = None) -> dict[str, str]:
    public_key, nonce_val, sig = sign_request(path, payload, nonce)
    return {
        "x-app-public-key": public_key,
        "x-app-nonce": str(nonce_val),
        "x-app-signed-data": sig,
        "Content-Type": "application/json",
        "User-Agent": "vchf-menace/1.0",
    }
