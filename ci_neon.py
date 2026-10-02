"""Run integration tests in a disposable schema on the dedicated Neon TEST branch."""
import os
import secrets
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote
import psycopg
from psycopg import sql

raw = os.environ["TEST_DATABASE_URL"]
parts = urlsplit(raw)
host = parts.netloc.replace("-pooler.", ".")
direct = urlunsplit((parts.scheme, host, parts.path, parts.query, ""))
schema = "ethos_ci_" + secrets.token_hex(8)
with psycopg.connect(direct, autocommit=True) as connection:
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
try:
    params = dict(parse_qsl(parts.query))
    params["options"] = "-csearch_path=" + schema
    env = dict(os.environ)
    env["ETHOS_TEST_DATABASE_URL"] = urlunsplit((parts.scheme, host, parts.path, urlencode(params), ""))
    process = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short"], cwd="release", env=env, capture_output=True, text=True)
    output = process.stdout + process.stderr
    for secret in [raw, direct, parts.password, unquote(parts.password or "")]:
        if secret:
            output = output.replace(secret, "[REDACTED]")
    print(output)
    code = process.returncode
finally:
    with psycopg.connect(direct, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
sys.exit(code)
