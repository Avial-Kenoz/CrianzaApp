"""mantenimiento: grupos de redundancia y criticidad efectiva

Revision ID: 20260930_02
Revises: 20260930_01
Create Date: 2026-09-30 00:00:00.000000

El respaldo «de a pares» (`mnt_equipos.respaldo_equipo_id`) no representaba
la realidad: los seis sopladores centralizados se cubren entre todos (N+1) y
la redundancia es simétrica, con el rol de «principal» rotando. Se reemplaza
por **grupos de redundancia** = el servicio que prestan varios equipos:

- `necesarios` (de día) y `necesarios_noche`: cuántos deben estar operando.
  Para los sopladores depende del oxígeno: de noche se necesitan más.
- `conmutacion`: manual / automática.
- `impacto` y `seguridad_ambiente`: la **consecuencia de perder el servicio**
  se responde una vez para el grupo (dos bombas gemelas no pueden tener
  criticidades distintas). La reposición sigue siendo de cada equipo.

La criticidad pasa a tener dos caras: **nominal** (con el grupo completo, la
que se guarda en `mnt_equipos.criticidad`) y **efectiva** (según cuántos del
grupo están operando ahora, calculada al vuelo).

**Expandir y después contraer.** Esta migración solo AGREGA: la columna
`respaldo_equipo_id` se conserva porque el proceso de producción que corre
todavía la usa. Se borra en una migración posterior, una vez desplegado el
código nuevo (y esa migración vuelve a convertir los vínculos que se hayan
creado entre medio).

Conversión de los respaldos existentes: cada componente conexo de vínculos
de respaldo es un grupo. Valores conservadores, para revisar en la UI:
- necesarios = miembros − 1 (N+1: con un equipo detenido queda sin respaldo);
- impacto = el más severo de sus miembros; seguridad = «sí» si alguno dijo sí;
- conmutación automática solo si todos respondieron «automático».
"""
import json
import re

from alembic import op
import sqlalchemy as sa


revision = "20260930_02"
down_revision = "20260930_01"
branch_labels = None
depends_on = None


def _nombre_grupo(nombres: list[str]) -> str:
    """«Soplador 1…6» → «Grupo Soplador»; nombres distintos → el del primero."""
    bases = {re.sub(r"\s*\d+\s*$", "", n).strip() for n in nombres}
    base = bases.pop() if len(bases) == 1 else nombres[0]
    return f"Grupo {base}"[:120]


def upgrade() -> None:
    op.create_table(
        "mnt_grupos_redundancia",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("sitio", sa.String(10), nullable=False),
        sa.Column("necesarios", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("necesarios_noche", sa.Integer()),
        sa.Column("conmutacion", sa.String(12), nullable=False, server_default="manual"),
        sa.Column("impacto", sa.String(1), nullable=False),
        sa.Column("seguridad_ambiente", sa.String(2), nullable=False),
        sa.Column("notas", sa.Text()),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nombre", name="uq_mnt_grupos_nombre"),
        sa.CheckConstraint("necesarios >= 1", name="ck_mnt_grupos_necesarios"),
        sa.CheckConstraint("conmutacion IN ('manual', 'automatica')", name="ck_mnt_grupos_conmutacion"),
    )
    op.add_column("mnt_equipos", sa.Column("grupo_id", sa.BigInteger(),
                                           sa.ForeignKey("mnt_grupos_redundancia.id"), nullable=True))
    op.create_index("ix_mnt_equipos_grupo", "mnt_equipos", ["grupo_id"])
    op.execute("INSERT INTO mnt_parametros (clave, valor, unidad, descripcion) VALUES "
               "('noche_desde', '20:00', 'hh:mm', 'Inicio de la noche: desde aquí rige «necesarios de noche» de cada grupo'), "
               "('noche_hasta', '08:00', 'hh:mm', 'Fin de la noche')")

    # --- convertir los vínculos de respaldo en grupos --------------------------
    con = op.get_bind()
    filas = con.execute(sa.text(
        "SELECT e.id, e.nombre, e.sitio, e.respaldo_equipo_id, "
        "  (SELECT respuestas FROM mnt_criticidad_evaluaciones v WHERE v.equipo_id = e.id "
        "   ORDER BY evaluado_at DESC, id DESC LIMIT 1) "
        "FROM mnt_equipos e ORDER BY e.id")).fetchall()
    info = {r[0]: {"nombre": r[1], "sitio": r[2], "resp": r[3],
                   "r": (r[4] if isinstance(r[4], dict) else json.loads(r[4] or "{}"))} for r in filas}
    # componentes conexos (unión-búsqueda) de los vínculos del mismo sitio
    padre = {i: i for i in info}

    def raiz(i):
        while padre[i] != i:
            padre[i] = padre[padre[i]]
            i = padre[i]
        return i

    for i, d in info.items():
        j = d["resp"]
        if j in info and info[j]["sitio"] == d["sitio"]:
            padre[raiz(i)] = raiz(j)
    comps: dict = {}
    for i in info:
        comps.setdefault(raiz(i), []).append(i)

    usados = set()
    for miembros in comps.values():
        if len(miembros) < 2:
            continue
        miembros.sort()
        rs = [info[m]["r"] for m in miembros]
        nombre = _nombre_grupo([info[m]["nombre"] for m in miembros])
        while nombre in usados:
            nombre += " (2)"
        usados.add(nombre)
        impacto = min((r.get("impacto") or "c") for r in rs)
        seguridad = "si" if any(r.get("seguridad_ambiente") == "si" for r in rs) else "no"
        conmutacion = "automatica" if all(r.get("respaldo") == "c" for r in rs) else "manual"
        gid = con.execute(sa.text(
            "INSERT INTO mnt_grupos_redundancia (nombre, sitio, necesarios, conmutacion, impacto, "
            "seguridad_ambiente, notas, activo, created_at, updated_at) VALUES "
            "(:n, :s, :nec, :c, :i, :seg, :notas, true, now(), now()) RETURNING id"),
            {"n": nombre, "s": info[miembros[0]]["sitio"], "nec": len(miembros) - 1, "c": conmutacion,
             "i": impacto, "seg": seguridad,
             "notas": "Creado al convertir los respaldos en pares. Revisar cuántos se necesitan."}).scalar()
        con.execute(sa.text("UPDATE mnt_equipos SET grupo_id = :g WHERE id = ANY(:ids)"),
                    {"g": gid, "ids": miembros})


def downgrade() -> None:
    op.execute("DELETE FROM mnt_parametros WHERE clave IN ('noche_desde', 'noche_hasta')")
    op.drop_index("ix_mnt_equipos_grupo", table_name="mnt_equipos")
    op.drop_column("mnt_equipos", "grupo_id")
    op.drop_table("mnt_grupos_redundancia")
