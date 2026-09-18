#!/usr/bin/env python
"""สร้าง VAPID key pair สำหรับ Web Push — รันครั้งเดียวต่อโปรเจกต์

ใช้:
    cd d:\tdapp\backend
    d:\tdapp\.venv\Scripts\python.exe scripts/gen_vapid_keys.py

จากนั้นคัดลอกค่าที่ได้ไปวางเป็น environment variable:
  - Render → บริการ tdapp-api → Environment:
        VAPID_PUBLIC_KEY  = <ค่า public>
        VAPID_PRIVATE_KEY = <ค่า private>
        VAPID_SUBJECT     = mailto:you@example.com   (อีเมลของคุณ)
    แล้ว Redeploy (api ~45-60 วิ)
  - เครื่อง local → backend/.env (ไฟล์นี้ไม่ถูก commit)

⚠️ Private key = ความลับเทียบเท่ารหัสผ่าน: ถ้าหลุด คนอื่นส่ง push
   เข้ามือถือผู้ใช้ได้ ⇒ เก็บใน env เท่านั้น ห้าม commit
⚠️ กุญแจต้อง "คู่เดียวกันเสมอ" — เปลี่ยน private key แล้ว subscription เดิม
   ทั้งหมดใช้ไม่ได้ ต้องให้ผู้ใช้กด "เปิดการแจ้งเตือน" ใหม่ทุกเครื่อง
"""
from __future__ import annotations

import base64
import sys

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except Exception as exc:  # pragma: no cover
    print("ต้องมีแพ็กเกจ cryptography (มากับ pywebpush):", exc)
    sys.exit(1)


def _b64url(raw: bytes) -> str:
    """Unpadded base64url — the format the Push API expects."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def main() -> None:
    key = ec.generate_private_key(ec.SECP256R1())

    # Private key: raw 32-byte scalar (what pywebpush accepts directly).
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    # Public key: uncompressed EC point (0x04 || X || Y) = 65 bytes, base64url.
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    print("=" * 68)
    print("VAPID key pair สำหรับ tdapp Web Push")
    print("=" * 68)
    print()
    print("VAPID_PUBLIC_KEY=" + _b64url(public_raw))
    print()
    print("VAPID_PRIVATE_KEY=" + _b64url(private_raw))
    print()
    print("VAPID_SUBJECT=mailto:your-email@example.com   # ← แก้เป็นอีเมลคุณ")
    print()
    print("-" * 68)
    print("ขั้นต่อไป:")
    print("  1) เพิ่ม env 3 ตัวข้างบนที่ Render → บริการ tdapp-api → Environment")
    print("  2) Redeploy บริการ api (~45-60 วิ)")
    print("  3) เปิดเว็บ → Settings → การแจ้งเตือนมือถือ → เปิดการแจ้งเตือน")
    print("  4) กดปุ่ม ทดสอบ แล้วดู notification tray ของมือถือ")
    print()
    print("หมายเหตุ: รันซ้ำจะได้กุญแจใหม่ — ของเดิมใช้ไม่ได้ทันที")
    print("=" * 68)


if __name__ == "__main__":
    main()
