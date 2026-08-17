"""创建公共地点库的最小权限运行账号。"""

from __future__ import annotations

import os

from psycopg import sql

RUNTIME_ROLE = "travelapp"
READER_ROLE = "catalogreader"


def provision_runtime_access() -> None:
    """幂等创建运行账号，并只授予地点目录读取权限。"""

    database_url = os.getenv("CATALOG_DATABASE_URL", "")
    password = os.getenv("TRAVELAPP_POSTGRES_PASSWORD", "")
    if not database_url or not password:
        raise RuntimeError("缺少地点库管理员连接或 travelapp 本地密码")

    import psycopg

    with psycopg.connect(database_url, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNTIME_ROLE,)
        ).fetchone()
        statement = (
            "ALTER ROLE {} LOGIN PASSWORD {}"
            if exists
            else "CREATE ROLE {} LOGIN PASSWORD {}"
        )
        connection.execute(
            sql.SQL(statement).format(sql.Identifier(RUNTIME_ROLE), sql.Literal(password))
        )
        connection.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(READER_ROLE), sql.Identifier(RUNTIME_ROLE)
            )
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(connection.info.dbname), sql.Identifier(RUNTIME_ROLE)
            )
        )


def main() -> None:
    """执行运行账号授权。"""

    provision_runtime_access()
    print("travelapp 运行账号已具备 catalog 只读权限。")


if __name__ == "__main__":
    main()
