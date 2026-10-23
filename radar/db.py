"""Postgres helpers."""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from .config import PG_DSN, ROOT, load_suppliers


@contextmanager
def connect():
    conn = psycopg.connect(PG_DSN, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema() -> None:
    """Create tables and upsert the supplier master from config/suppliers.yaml."""
    with connect() as conn:
        conn.execute((ROOT / "db" / "schema.sql").read_text())
        for s in load_suppliers():
            conn.execute(
                """INSERT INTO suppliers (id, name, category, country, tier, criticality)
                   VALUES (%(id)s, %(name)s, %(category)s, %(country)s, %(tier)s, %(criticality)s)
                   ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, category=EXCLUDED.category,
                     country=EXCLUDED.country, tier=EXCLUDED.tier, criticality=EXCLUDED.criticality""",
                s,
            )


if __name__ == "__main__":
    init_schema()
    print("Schema ready.")
