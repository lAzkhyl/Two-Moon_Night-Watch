# =====
# MODULE: db/migrate.py
# =====
# Architecture Overview:
# An automated database migration engine. It compares the current state
# of the 'schema_migrations' table against the local .sql files residing
# in the 'db/migrations/' directory. If new migrations are found, they
# are executed sequentially to keep the PostgreSQL schema up-to-date.
# =====

import logging
import asyncpg
from pathlib import Path

logger = logging.getLogger(__name__)


async def run_migrations(pool: asyncpg.Pool):
    # -----
    # Bootstraps the migration table if missing, then iterates over
    # locally stored SQL files, executing only the missing ones.
    # Each migration is wrapped in its own transaction — a failure in
    # one migration is logged and re-raised, but does NOT silently skip.
    # -----
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        applied = {r["version"] for r in await conn.fetch(
            "SELECT version FROM schema_migrations"
        )}
        logger.info(f"[Migrations] Already applied: {sorted(applied) or 'none'}")

        migration_dir = Path(__file__).parent / "migrations"
        migration_files = sorted(migration_dir.glob("*.sql"))

        if not migration_files:
            logger.warning("[Migrations] No .sql files found in migrations directory.")
            return

        pending = [
            f for f in migration_files
            if int(f.stem.split("_")[0]) not in applied
        ]

        if not pending:
            logger.info("[Migrations] Schema is up-to-date. No pending migrations.")
            return

        logger.info(f"[Migrations] {len(pending)} pending migration(s): {[f.name for f in pending]}")

        for migration_file in pending:
            version = int(migration_file.stem.split("_")[0])
            logger.info(f"[Migrations] Applying migration {migration_file.name} ...")

            sql = migration_file.read_text(encoding="utf-8")

            # BUG FIX: Detect and reject SQL containing embedded null bytes.
            # Migration 005 was previously saved as UTF-16 LE (null byte after
            # every character), which PostgreSQL rejects with a syntax error.
            # This guard provides a clear diagnostic instead of a cryptic DB error.
            if "\x00" in sql:
                raise ValueError(
                    f"[Migrations] FATAL: Migration file '{migration_file.name}' contains embedded "
                    f"null bytes (\\x00). The file is likely saved as UTF-16 instead of UTF-8. "
                    f"Re-save the file as UTF-8 without BOM and retry."
                )

            # CVE-2M-020: Wrap each migration in a transaction for atomicity
            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)", version
                    )
                logger.info(f"[Migrations] ✅ Migration {migration_file.name} applied successfully.")
            except Exception as exc:
                logger.error(
                    f"[Migrations] ❌ Migration {migration_file.name} FAILED. "
                    f"Transaction rolled back. Error: {exc}"
                )
                raise  # Re-raise so the caller (on_ready) knows migrations are incomplete