"""
ETL: Carga datos legacy desde 'Copia de acuicola.sql' a fastapp_etapa1.

Estrategia:
- Se mantienen los IDs originales del dump (id = legacy_id).
- FKs funcionan directamente sin remapeo.
- Se resetean las secuencias al final.
- Las tablas con contraseñas (users) se insertan sin el campo password.
"""

import os
import io
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DUMP_FILE = os.path.join(os.path.dirname(__file__), "Copia de acuicola.sql")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://fastapp_user:fastapp_pass@localhost/fastapp_etapa1")

# Parsear la URL manualmente para psycopg2
import re
m = re.match(r"postgresql://(\w+):(\w+)@([\w.]+)/(\w+)", DATABASE_URL)
DB_PARAMS = dict(user=m.group(1), password=m.group(2), host=m.group(3), dbname=m.group(4))


def parse_copy_block(dump_path: str, table_name: str) -> tuple[list[str], list[list[str]]]:
    """Extrae encabezados y filas de un bloque COPY del dump."""
    headers = []
    rows = []
    inside = False

    with open(dump_path, "r", encoding="utf-8") as f:
        for line in f:
            if not inside:
                if line.startswith(f"COPY public.{table_name} ("):
                    # Extraer nombres de columnas
                    cols_str = re.search(r"\((.+?)\)", line).group(1)
                    headers = [c.strip().strip('"') for c in cols_str.split(",")]
                    inside = True
            else:
                if line.startswith("\\."):
                    break
                rows.append(line.rstrip("\n").split("\t"))

    return headers, rows


def null_val(v: str):
    return None if v == "\\N" else v


def bool_val(v: str):
    if v is None or v == "\\N":
        return None
    return v == "t"


def execute_copy(conn, table: str, columns: list[str], rows: list[list[str]]):
    """Inserta filas usando COPY FROM STDIN (rápido para grandes volúmenes)."""
    if not rows:
        print(f"  {table}: sin filas, saltando.")
        return

    cur = conn.cursor()
    # Construir buffer en formato texto para COPY
    buf = io.StringIO()
    for row in rows:
        buf.write("\t".join("\\N" if v is None else str(v) for v in row) + "\n")
    buf.seek(0)

    cols = ", ".join(f'"{c}"' for c in columns)
    cur.copy_expert(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT text, NULL '\\N')", buf)
    conn.commit()
    print(f"  {table}: {len(rows)} filas cargadas.")
    cur.close()


def load_cultivation_units(conn):
    src_cols, rows = parse_copy_block(DUMP_FILE, "cultivation_units")
    # src: id, name, created_at, updated_at
    # dst: id, legacy_id, name, created_at, updated_at
    out_cols = ["id", "legacy_id", "name", "created_at", "updated_at"]
    out_rows = []
    for r in rows:
        id_, name, created_at, updated_at = r
        out_rows.append([id_, id_, name, created_at, updated_at])
    execute_copy(conn, "cultivation_units", out_cols, out_rows)


def load_pond_types(conn):
    src_cols, rows = parse_copy_block(DUMP_FILE, "pond_types")
    # src: id, name, created_at, updated_at
    out_cols = ["id", "legacy_id", "name", "created_at", "updated_at"]
    out_rows = []
    for r in rows:
        id_, name, created_at, updated_at = r
        out_rows.append([id_, id_, name, created_at, updated_at])
    execute_copy(conn, "pond_types", out_cols, out_rows)


def load_species(conn):
    src_cols, rows = parse_copy_block(DUMP_FILE, "species")
    # src: id, name, internal_id, created_at, updated_at
    out_cols = ["id", "legacy_id", "name", "internal_id", "created_at", "updated_at"]
    out_rows = []
    for r in rows:
        id_, name, internal_id, created_at, updated_at = r
        out_rows.append([id_, id_, name, null_val(internal_id), created_at, updated_at])
    execute_copy(conn, "species", out_cols, out_rows)


def load_roles(conn):
    src_cols, rows = parse_copy_block(DUMP_FILE, "roles")
    # src: id, name, resource_type, resource_id, created_at, updated_at
    out_cols = ["id", "legacy_id", "name", "created_at", "updated_at"]
    out_rows = []
    for r in rows:
        id_, name, _rtype, _rid, created_at, updated_at = r
        out_rows.append([id_, id_, name, created_at, updated_at])
    execute_copy(conn, "roles", out_cols, out_rows)


def load_lots(conn):
    src_cols, rows = parse_copy_block(DUMP_FILE, "lots")
    # src: id, species_id, name, internal_id, origin, hatch_year, creation_time,
    #       created_at, updated_at, creation_date, "national"
    out_cols = ["id", "legacy_id", "species_id", "name", "internal_id", "origin",
                "hatch_year", "creation_time", "created_at", "updated_at",
                "creation_date", "national"]
    out_rows = []
    for r in rows:
        id_, species_id, name, internal_id, origin, hatch_year, creation_time, \
            created_at, updated_at, creation_date, national = r
        out_rows.append([
            id_, id_, species_id, name,
            null_val(internal_id), null_val(origin),
            null_val(hatch_year),
            null_val(creation_time), created_at, updated_at,
            null_val(creation_date),
            bool_val(national) if national != "\\N" else None,
        ])
    execute_copy(conn, "lots", out_cols, out_rows)


def load_users(conn):
    src_cols, rows = parse_copy_block(DUMP_FILE, "users")
    # src: id, email, encrypted_password, reset_password_token, reset_password_sent_at,
    #       remember_created_at, created_at, updated_at, name, lastname, rut
    # dst: id, legacy_id, email, name, lastname, rut, active, created_at, updated_at
    out_cols = ["id", "legacy_id", "email", "name", "lastname", "rut", "active",
                "created_at", "updated_at"]
    out_rows = []
    for r in rows:
        id_, email, _pwd, _rpt, _rps, _rca, created_at, updated_at, name, lastname, rut = r
        out_rows.append([
            id_, id_, email,
            null_val(name), null_val(lastname), null_val(rut),
            True,
            created_at, updated_at,
        ])
    execute_copy(conn, "users", out_cols, out_rows)


def load_ponds(conn):
    src_cols, rows = parse_copy_block(DUMP_FILE, "ponds")
    # src: id, name, internal_id, cultivation_unit_id, pond_type_id, volume,
    #       depuration, created_at, updated_at, code, state, lot_id, biomass, avg_weight
    out_cols = ["id", "legacy_id", "name", "internal_id", "cultivation_unit_id",
                "pond_type_id", "volume", "depuration", "created_at", "updated_at",
                "code", "state", "lot_id", "biomass", "avg_weight"]
    out_rows = []
    for r in rows:
        id_, name, internal_id, cu_id, pt_id, volume, depuration, \
            created_at, updated_at, code, state, lot_id, biomass, avg_weight = r
        out_rows.append([
            id_, id_, name, null_val(internal_id), cu_id, pt_id,
            null_val(volume),
            bool_val(depuration) if depuration != "\\N" else False,
            created_at, updated_at,
            null_val(code), null_val(state),
            null_val(lot_id), null_val(biomass), null_val(avg_weight),
        ])
    execute_copy(conn, "ponds", out_cols, out_rows)


def load_users_roles(conn):
    src_cols, rows = parse_copy_block(DUMP_FILE, "users_roles")
    # src: user_id, role_id
    out_cols = ["user_id", "role_id"]
    execute_copy(conn, "users_roles", out_cols, rows)


def load_fish(conn):
    src_cols, rows = parse_copy_block(DUMP_FILE, "fish")
    # src: id, internal_id, lot_id, sex, state, devious_time, registration_time,
    #       created_at, updated_at, date_of_death, dupuration_start_time, mortality_id
    out_cols = ["id", "legacy_id", "internal_id", "lot_id", "sex", "state",
                "devious_time", "registration_time", "created_at", "updated_at",
                "date_of_death", "depuration_start_time", "mortality_id"]
    out_rows = []
    for r in rows:
        id_, internal_id, lot_id, sex, state, devious_time, registration_time, \
            created_at, updated_at, date_of_death, dup_start, mortality_id = r
        out_rows.append([
            id_, id_, internal_id, lot_id,
            null_val(sex), null_val(state),
            null_val(devious_time), null_val(registration_time),
            created_at, updated_at,
            null_val(date_of_death), null_val(dup_start),
            null_val(mortality_id),
        ])
    execute_copy(conn, "fish", out_cols, out_rows)


def load_ponds_movements(conn):
    src_cols, rows = parse_copy_block(DUMP_FILE, "ponds_movements")
    # src: id, source_pond_id, destiny_pond_id, fish_quantity, lot_id,
    #       movement_reason, movement_time, created_at, updated_at, fish_id, folio
    out_cols = ["id", "legacy_id", "source_pond_id", "destiny_pond_id",
                "fish_quantity", "lot_id", "movement_reason", "movement_time",
                "created_at", "updated_at", "fish_id", "folio"]
    out_rows = []
    for r in rows:
        id_, src_pond, dst_pond, fish_qty, lot_id, reason, mv_time, \
            created_at, updated_at, fish_id, folio = r
        out_rows.append([
            id_, id_,
            null_val(src_pond), null_val(dst_pond),
            fish_qty, null_val(lot_id),
            reason, mv_time, created_at, updated_at,
            null_val(fish_id), null_val(folio),
        ])
    execute_copy(conn, "ponds_movements", out_cols, out_rows)


def reset_sequences(conn):
    """Resetea todas las secuencias al máximo id de cada tabla."""
    tables = ["cultivation_units", "pond_types", "species", "roles", "lots",
              "users", "ponds", "fish", "ponds_movements"]
    cur = conn.cursor()
    for t in tables:
        cur.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), COALESCE(MAX(id), 1)) FROM {t}")
    conn.commit()
    cur.close()
    print("\nSecuencias reseteadas.")


def main():
    print(f"Conectando a {DB_PARAMS['dbname']}@{DB_PARAMS['host']}...")
    conn = psycopg2.connect(**DB_PARAMS)

    # Insertar en orden de dependencias (respeta FKs sin desactivarlas)
    print("\nCargando catálogos base (sin FK)...")
    load_cultivation_units(conn)
    load_pond_types(conn)
    load_species(conn)
    load_roles(conn)

    print("\nCargando lots y users...")
    load_lots(conn)
    load_users(conn)

    print("\nCargando ponds y users_roles...")
    load_ponds(conn)
    load_users_roles(conn)

    print("\nCargando fish (47k filas)...")
    load_fish(conn)

    print("\nCargando ponds_movements (159k filas)...")
    load_ponds_movements(conn)

    reset_sequences(conn)
    conn.close()
    print("\nETL completado exitosamente.")


if __name__ == "__main__":
    main()
