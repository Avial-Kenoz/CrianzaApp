from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Iterable

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DUMP_FILE = PROJECT_ROOT / "acuicola_05052026.sql"
CUTOFF_DATE = date(2026, 4, 27)


TARGET_TABLES = {
    "ponds",
    "fish",
    "ponds_movements",
    "fish_female_samplings",
    "depuration_periodic_samplings",
    "mortality_reports",
    "marked_mortality_registries",
    "unmarked_mortality_registries",
}


def parse_date_prefix(value: str | None) -> date | None:
    if not value or value == "\\N":
        return None
    raw = value.strip()
    if len(raw) < 10:
        return None
    try:
        y = int(raw[0:4])
        m = int(raw[5:7])
        d = int(raw[8:10])
        return date(y, m, d)
    except Exception:
        return None


def is_since_cutoff(row: dict[str, str], fields: Iterable[str]) -> bool:
    for field in fields:
        dt = parse_date_prefix(row.get(field))
        if dt is not None and dt >= CUTOFF_DATE:
            return True
    return False


def nullify(v: str | None):
    if v is None or v == "\\N":
        return None
    return v


def as_bool(v: str | None):
    if v is None or v == "\\N":
        return None
    if v == "t":
        return True
    if v == "f":
        return False
    return None


def parse_dump_copy_blocks(dump_path: Path):
    active_table = None
    active_cols: list[str] = []

    with dump_path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if active_table is None:
                s = line.strip()
                if not s.startswith("COPY public.") or " FROM stdin;" not in s:
                    continue
                token = s.split(" ", 2)[1]  # public.table_name
                if "." not in token:
                    continue
                table_name = token.split(".", 1)[1]
                if table_name not in TARGET_TABLES:
                    continue
                open_idx = s.find("(")
                close_idx = s.rfind(")")
                if open_idx == -1 or close_idx == -1 or close_idx <= open_idx:
                    continue
                cols_part = s[open_idx + 1 : close_idx]
                active_table = table_name
                active_cols = [c.strip().strip('"') for c in cols_part.split(",")]
                continue

            if line.startswith("\\."):
                active_table = None
                active_cols = []
                continue

            values = line.rstrip("\r\n").split("\t")
            if len(values) != len(active_cols):
                if len(values) < len(active_cols):
                    values += ["\\N"] * (len(active_cols) - len(values))
                else:
                    values = values[: len(active_cols)]
            yield active_table, dict(zip(active_cols, values))


def upsert_rows(conn, table: str, columns: list[str], rows: list[tuple], conflict_col: str = "id"):
    if not rows:
        return
    assignments = ", ".join([f"{c}=EXCLUDED.{c}" for c in columns if c != conflict_col])
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s "
        f"ON CONFLICT ({conflict_col}) DO UPDATE SET {assignments}"
    )
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=1000)


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    db_url = os.getenv("DATABASE_URL", "postgresql://fastapp_user:fastapp_pass@localhost/fastapp_etapa1")

    if not DUMP_FILE.exists():
        raise FileNotFoundError(f"No existe dump: {DUMP_FILE}")

    rows_ponds = []
    rows_fish = []
    rows_movements = []
    rows_female_samples = []
    rows_depuration_samples = []
    rows_mortality_reports = []
    rows_marked = []
    rows_unmarked = []

    for table, row in parse_dump_copy_blocks(DUMP_FILE):
        if table == "ponds":
            if not is_since_cutoff(row, ["updated_at", "created_at"]):
                continue
            rows_ponds.append(
                (
                    int(row["id"]),
                    int(row["id"]),
                    nullify(row.get("name")),
                    nullify(row.get("internal_id")),
                    int(row["cultivation_unit_id"]) if row.get("cultivation_unit_id") not in (None, "\\N") else None,
                    int(row["pond_type_id"]) if row.get("pond_type_id") not in (None, "\\N") else None,
                    int(row["volume"]) if row.get("volume") not in (None, "\\N") else None,
                    as_bool(row.get("depuration")) if row.get("depuration") != "\\N" else False,
                    nullify(row.get("created_at")),
                    nullify(row.get("updated_at")),
                    nullify(row.get("code")),
                    nullify(row.get("state")),
                    int(row["lot_id"]) if row.get("lot_id") not in (None, "\\N") else None,
                    nullify(row.get("biomass")),
                    nullify(row.get("avg_weight")),
                )
            )

        elif table == "fish":
            if not is_since_cutoff(row, ["updated_at", "registration_time", "devious_time", "date_of_death", "dupuration_start_time"]):
                continue
            rows_fish.append(
                (
                    int(row["id"]),
                    int(row["id"]),
                    nullify(row.get("internal_id")),
                    int(row["lot_id"]) if row.get("lot_id") not in (None, "\\N") else None,
                    nullify(row.get("sex")),
                    nullify(row.get("state")),
                    nullify(row.get("devious_time")),
                    nullify(row.get("registration_time")),
                    nullify(row.get("created_at")),
                    nullify(row.get("updated_at")),
                    nullify(row.get("date_of_death")),
                    nullify(row.get("dupuration_start_time")),
                    nullify(row.get("mortality_id")),
                )
            )

        elif table == "ponds_movements":
            if not is_since_cutoff(row, ["movement_time", "updated_at", "created_at"]):
                continue
            rows_movements.append(
                (
                    int(row["id"]),
                    int(row["id"]),
                    int(row["source_pond_id"]) if row.get("source_pond_id") not in (None, "\\N") else None,
                    int(row["destiny_pond_id"]) if row.get("destiny_pond_id") not in (None, "\\N") else None,
                    int(row["fish_quantity"]) if row.get("fish_quantity") not in (None, "\\N") else 1,
                    int(row["lot_id"]) if row.get("lot_id") not in (None, "\\N") else None,
                    nullify(row.get("movement_reason")),
                    nullify(row.get("movement_time")),
                    nullify(row.get("created_at")),
                    nullify(row.get("updated_at")),
                    int(row["fish_id"]) if row.get("fish_id") not in (None, "\\N") else None,
                    int(row["folio"]) if row.get("folio") not in (None, "\\N") else None,
                )
            )

        elif table == "fish_female_samplings":
            if not is_since_cutoff(row, ["registry_time", "updated_at", "created_at"]):
                continue
            rows_female_samples.append(
                (
                    int(row["id"]),
                    int(row["id"]),
                    int(row["user_id"]) if row.get("user_id") not in (None, "\\N") else None,
                    int(row["fish_id"]) if row.get("fish_id") not in (None, "\\N") else None,
                    nullify(row.get("length")),
                    nullify(row.get("weight")),
                    nullify(row.get("oocyte_size")),
                    nullify(row.get("color")),
                    nullify(row.get("gonad_size")),
                    nullify(row.get("flavor")),
                    nullify(row.get("development_state")),
                    nullify(row.get("harvest_date")),
                    nullify(row.get("registry_time")),
                    nullify(row.get("diameter")),
                    int(row["agglomeration"]) if row.get("agglomeration") not in (None, "\\N") else None,
                    int(row["turgor"]) if row.get("turgor") not in (None, "\\N") else None,
                    int(row["fat"]) if row.get("fat") not in (None, "\\N") else None,
                    nullify(row.get("created_at")),
                    nullify(row.get("updated_at")),
                )
            )

        elif table == "depuration_periodic_samplings":
            if not is_since_cutoff(row, ["updated_at", "created_at", "harvest_date"]):
                continue
            rows_depuration_samples.append(
                (
                    int(row["id"]),
                    int(row["id"]),
                    int(row["fish_id"]) if row.get("fish_id") not in (None, "\\N") else None,
                    int(row["pond_id"]) if row.get("pond_id") not in (None, "\\N") else None,
                    nullify(row.get("development_state")),
                    nullify(row.get("width")),
                    nullify(row.get("heigh")),
                    nullify(row.get("weight")),
                    nullify(row.get("harvest_date")),
                    nullify(row.get("color")),
                    nullify(row.get("gonad_dimension")),
                    nullify(row.get("oocyte_size")),
                    nullify(row.get("flavor")),
                    nullify(row.get("created_at")),
                    nullify(row.get("updated_at")),
                )
            )

        elif table == "mortality_reports":
            if not is_since_cutoff(row, ["registry_time", "updated_at", "created_at"]):
                continue
            rows_mortality_reports.append(
                (
                    int(row["id"]),
                    int(row["id"]),
                    nullify(row.get("comment")),
                    nullify(row.get("registry_time")),
                    int(row["breeding_user_id"]) if row.get("breeding_user_id") not in (None, "\\N") else None,
                    int(row["supervisor_user_id"]) if row.get("supervisor_user_id") not in (None, "\\N") else None,
                    int(row["general_user_id"]) if row.get("general_user_id") not in (None, "\\N") else None,
                    as_bool(row.get("supervisor_validation")) if row.get("supervisor_validation") != "\\N" else False,
                    as_bool(row.get("general_validation")) if row.get("general_validation") != "\\N" else False,
                    nullify(row.get("supervisor_validation_time")),
                    nullify(row.get("general_validation_time")),
                    nullify(row.get("created_at")),
                    nullify(row.get("updated_at")),
                )
            )

        elif table == "marked_mortality_registries":
            if not is_since_cutoff(row, ["registry_time", "updated_at", "created_at"]):
                continue
            rows_marked.append(
                (
                    int(row["id"]),
                    int(row["id"]),
                    int(row["mortality_report_id"]),
                    int(row["ponds_movement_id"]) if row.get("ponds_movement_id") not in (None, "\\N") else None,
                    nullify(row.get("comment")),
                    nullify(row.get("registry_time")),
                    as_bool(row.get("supervisor_validation")) if row.get("supervisor_validation") != "\\N" else False,
                    as_bool(row.get("general_validation")) if row.get("general_validation") != "\\N" else False,
                    nullify(row.get("created_at")),
                    nullify(row.get("updated_at")),
                )
            )

        elif table == "unmarked_mortality_registries":
            if not is_since_cutoff(row, ["registry_time", "updated_at", "created_at"]):
                continue
            rows_unmarked.append(
                (
                    int(row["id"]),
                    int(row["id"]),
                    int(row["mortality_report_id"]),
                    int(row["ponds_movement_id"]) if row.get("ponds_movement_id") not in (None, "\\N") else None,
                    nullify(row.get("comment")),
                    nullify(row.get("registry_time")),
                    as_bool(row.get("supervisor_validation")) if row.get("supervisor_validation") != "\\N" else False,
                    as_bool(row.get("general_validation")) if row.get("general_validation") != "\\N" else False,
                    nullify(row.get("created_at")),
                    nullify(row.get("updated_at")),
                )
            )

    print("Filas capturadas desde dump:")
    print(f"  ponds={len(rows_ponds)}")
    print(f"  fish={len(rows_fish)}")
    print(f"  ponds_movements={len(rows_movements)}")
    print(f"  fish_female_samplings=>fish_samplings={len(rows_female_samples)}")
    print(f"  depuration_periodic_samplings={len(rows_depuration_samples)}")
    print(f"  mortality_reports={len(rows_mortality_reports)}")
    print(f"  marked_mortality_registries={len(rows_marked)}")
    print(f"  unmarked_mortality_registries={len(rows_unmarked)}")

    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            # Borrar tramo temporal inventado / previo en tablas de eventos
            cur.execute("DELETE FROM marked_mortality_registries WHERE registry_time::date >= %s", (CUTOFF_DATE,))
            cur.execute("DELETE FROM unmarked_mortality_registries WHERE registry_time::date >= %s", (CUTOFF_DATE,))
            cur.execute("DELETE FROM mortality_reports WHERE registry_time::date >= %s", (CUTOFF_DATE,))
            cur.execute("DELETE FROM fish_samplings WHERE COALESCE(registry_time, created_at)::date >= %s", (CUTOFF_DATE,))
            cur.execute("DELETE FROM depuration_periodic_samplings WHERE COALESCE(updated_at, created_at)::date >= %s", (CUTOFF_DATE,))
            cur.execute("DELETE FROM ponds_movements WHERE movement_time::date >= %s", (CUTOFF_DATE,))

            # Limpiar sesiones de muestreo app (no legacy) en el tramo
            cur.execute(
                """
                DELETE FROM sampling_records
                WHERE session_id IN (
                    SELECT id FROM sampling_sessions
                    WHERE registry_date >= %s OR COALESCE(closed_at, created_at)::date >= %s
                )
                """,
                (CUTOFF_DATE, CUTOFF_DATE),
            )
            cur.execute(
                "DELETE FROM sampling_sessions WHERE registry_date >= %s OR COALESCE(closed_at, created_at)::date >= %s",
                (CUTOFF_DATE, CUTOFF_DATE),
            )

            # Limpiar tablas app-especificas generadas en ese tramo
            cur.execute("DELETE FROM cultivation_declaration_items WHERE COALESCE(updated_at, created_at)::date >= %s", (CUTOFF_DATE,))
            cur.execute("DELETE FROM cultivation_declarations WHERE COALESCE(updated_at, created_at)::date >= %s", (CUTOFF_DATE,))
            cur.execute("DELETE FROM fish_drug_uses WHERE COALESCE(updated_at, created_at)::date >= %s", (CUTOFF_DATE,))
            cur.execute("DELETE FROM tag_detachment_events WHERE created_at::date >= %s", (CUTOFF_DATE,))

        if rows_ponds:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    UPDATE ponds
                    SET
                        legacy_id = %s,
                        name = %s,
                        internal_id = %s,
                        cultivation_unit_id = %s,
                        pond_type_id = %s,
                        volume = %s,
                        depuration = COALESCE(%s, depuration),
                        created_at = COALESCE(%s, created_at),
                        updated_at = COALESCE(%s, updated_at),
                        code = %s,
                        state = %s,
                        lot_id = %s,
                        biomass = %s,
                        avg_weight = %s
                    WHERE id = %s
                    """,
                    [
                        (
                            r[1],
                            r[2],
                            r[3],
                            r[4],
                            r[5],
                            r[6],
                            r[7],
                            r[8],
                            r[9],
                            r[10],
                            r[11],
                            r[12],
                            r[13],
                            r[14],
                            r[0],
                        )
                        for r in rows_ponds
                    ],
                )

        upsert_rows(
            conn,
            "fish",
            [
                "id",
                "legacy_id",
                "internal_id",
                "lot_id",
                "sex",
                "state",
                "devious_time",
                "registration_time",
                "created_at",
                "updated_at",
                "date_of_death",
                "depuration_start_time",
                "mortality_id",
            ],
            rows_fish,
        )

        upsert_rows(
            conn,
            "ponds_movements",
            [
                "id",
                "legacy_id",
                "source_pond_id",
                "destiny_pond_id",
                "fish_quantity",
                "lot_id",
                "movement_reason",
                "movement_time",
                "created_at",
                "updated_at",
                "fish_id",
                "folio",
            ],
            rows_movements,
        )

        upsert_rows(
            conn,
            "fish_samplings",
            [
                "id",
                "legacy_id",
                "user_id",
                "fish_id",
                "length",
                "weight",
                "oocyte_size",
                "color",
                "gonad_size",
                "flavor",
                "development_state",
                "harvest_date",
                "registry_time",
                "diameter",
                "agglomeration",
                "turgor",
                "fat",
                "created_at",
                "updated_at",
            ],
            rows_female_samples,
        )

        upsert_rows(
            conn,
            "depuration_periodic_samplings",
            [
                "id",
                "legacy_id",
                "fish_id",
                "pond_id",
                "development_state",
                "width",
                "heigh",
                "weight",
                "harvest_date",
                "color",
                "gonad_dimension",
                "oocyte_size",
                "flavor",
                "created_at",
                "updated_at",
            ],
            rows_depuration_samples,
        )

        upsert_rows(
            conn,
            "mortality_reports",
            [
                "id",
                "legacy_id",
                "comment",
                "registry_time",
                "breeding_user_id",
                "supervisor_user_id",
                "general_user_id",
                "supervisor_validation",
                "general_validation",
                "supervisor_validation_time",
                "general_validation_time",
                "created_at",
                "updated_at",
            ],
            rows_mortality_reports,
        )

        upsert_rows(
            conn,
            "marked_mortality_registries",
            [
                "id",
                "legacy_id",
                "mortality_report_id",
                "ponds_movement_id",
                "comment",
                "registry_time",
                "supervisor_validation",
                "general_validation",
                "created_at",
                "updated_at",
            ],
            rows_marked,
        )

        upsert_rows(
            conn,
            "unmarked_mortality_registries",
            [
                "id",
                "legacy_id",
                "mortality_report_id",
                "ponds_movement_id",
                "comment",
                "registry_time",
                "supervisor_validation",
                "general_validation",
                "created_at",
                "updated_at",
            ],
            rows_unmarked,
        )

        with conn.cursor() as cur:
            cur.execute("SELECT setval(pg_get_serial_sequence('ponds_movements','id'), COALESCE((SELECT MAX(id) FROM ponds_movements), 1))")
            cur.execute("SELECT setval(pg_get_serial_sequence('fish_samplings','id'), COALESCE((SELECT MAX(id) FROM fish_samplings), 1))")
            cur.execute("SELECT setval(pg_get_serial_sequence('mortality_reports','id'), COALESCE((SELECT MAX(id) FROM mortality_reports), 1))")
            cur.execute("SELECT setval(pg_get_serial_sequence('marked_mortality_registries','id'), COALESCE((SELECT MAX(id) FROM marked_mortality_registries), 1))")
            cur.execute("SELECT setval(pg_get_serial_sequence('unmarked_mortality_registries','id'), COALESCE((SELECT MAX(id) FROM unmarked_mortality_registries), 1))")
            cur.execute("SELECT setval(pg_get_serial_sequence('depuration_periodic_samplings','id'), COALESCE((SELECT MAX(id) FROM depuration_periodic_samplings), 1))")

        conn.commit()
        print("Sincronizacion incremental completada desde 2026-04-27.")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
