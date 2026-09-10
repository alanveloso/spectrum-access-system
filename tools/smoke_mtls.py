"""Smoke: start UUT briefly and probe mTLS on RSA and ECC listeners."""

from __future__ import annotations

import argparse
import os
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from tools.winnforum.healthcheck import wait_for_mtls_admin


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def probe_rejects_missing_client_cert(*, base_url: str, ca_certs: Path) -> bool:
    """Return True when the server refuses a connection without a client certificate."""
    ctx = ssl.create_default_context(cafile=str(ca_certs))
    # Intentionally do not load a client cert/key.
    url = base_url.rstrip("/") + "/admin/get_daily_activities_status"
    try:
        req = Request(
            url,
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, context=ctx, timeout=5) as resp:
            # Any HTTP success means mTLS was not enforced.
            code = getattr(resp, "status", None) or resp.getcode()
            print(f"unexpected_success_without_client_cert HTTP {code} from {url}")
            return False
    except ssl.SSLError:
        return True
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLError):
            return True
        # Connection reset / handshake failure also counts as rejection.
        text = str(reason).lower()
        if "certificate" in text or "ssl" in text or "handshake" in text:
            return True
        print(f"unexpected_error_without_client_cert: {exc}")
        return False
    except OSError as exc:
        text = str(exc).lower()
        if "certificate" in text or "ssl" in text:
            return True
        print(f"unexpected_oserror_without_client_cert: {exc}")
        return False


def run_smoke(
    *,
    certs_dir: Path,
    host: str = "localhost",
    rsa_port: int = 19000,
    ecc_port: int = 19001,
    timeout_seconds: float = 60.0,
) -> int:
    certs_dir = certs_dir.resolve()
    client_cert = certs_dir / "client.cert"
    client_key = certs_dir / "client.key"
    ca_certs = certs_dir / "ca.cert"
    for path in (client_cert, client_key, ca_certs):
        if not path.is_file():
            print(f"missing {path}", file=sys.stderr)
            return 2

    env = os.environ.copy()
    env["CERTS_DIR"] = str(certs_dir)
    env["RSA_PORT"] = str(rsa_port)
    env["ECC_PORT"] = str(ecc_port)
    # Bind all interfaces so localhost/127.0.0.1 both work in CI.
    env["API_HOST"] = "0.0.0.0"
    # Avoid requiring RabbitMQ for listener bind smoke.
    env.setdefault("SAS_EXECUTION_MODE", "certification")
    artifacts = _repo_root() / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    env.setdefault("DATABASE_URL", f"sqlite:///{artifacts / 'ci-smoke.db'}")

    log_path = artifacts / "ci-smoke-uut.log"
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=str(_repo_root()),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # Fail fast if the UUT exits before listeners are ready.
        deadline = time.monotonic() + min(5.0, timeout_seconds)
        while time.monotonic() < deadline:
            rc = proc.poll()
            if rc is not None:
                print(f"uut_exited_early code={rc}", file=sys.stderr)
                return 1
            time.sleep(0.1)

        rsa = wait_for_mtls_admin(
            base_url=f"https://{host}:{rsa_port}",
            ca_certs=ca_certs,
            client_cert=client_cert,
            client_key=client_key,
            timeout_seconds=timeout_seconds,
        )
        ecc = wait_for_mtls_admin(
            base_url=f"https://{host}:{ecc_port}",
            ca_certs=ca_certs,
            client_cert=client_cert,
            client_key=client_key,
            timeout_seconds=timeout_seconds,
        )
        print(f"rsa_ok={rsa.ok} detail={rsa.detail}")
        print(f"ecc_ok={ecc.ok} detail={ecc.detail}")
        if not (rsa.ok and ecc.ok):
            return 1

        rsa_reject = probe_rejects_missing_client_cert(
            base_url=f"https://{host}:{rsa_port}", ca_certs=ca_certs
        )
        ecc_reject = probe_rejects_missing_client_cert(
            base_url=f"https://{host}:{ecc_port}", ca_certs=ca_certs
        )
        print(f"rsa_rejects_missing_client={rsa_reject}")
        print(f"ecc_rejects_missing_client={ecc_reject}")
        return 0 if rsa_reject and ecc_reject else 1
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_file.close()
        # Give sockets a moment to release in CI runners.
        time.sleep(0.2)
        try:
            leftover = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            leftover = ""
        if leftover:
            print("--- uut log ---")
            print(leftover[-4000:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--certs-dir", type=Path, required=True)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--rsa-port", type=int, default=19000)
    parser.add_argument("--ecc-port", type=int, default=19001)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    return run_smoke(
        certs_dir=args.certs_dir,
        host=args.host,
        rsa_port=args.rsa_port,
        ecc_port=args.ecc_port,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
