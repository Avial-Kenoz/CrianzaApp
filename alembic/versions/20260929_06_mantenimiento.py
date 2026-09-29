"""módulo de mantenimiento de maquinaria (PR1)

Revision ID: 20260929_06
Revises: 20260929_05
Create Date: 2026-09-29 00:00:00.000000

Crea el módulo completo (ver especificacion_mantenimiento_v1.md §3), aunque en
PR1 solo se usan el catálogo, las personas, los contratistas, la criticidad y
los parámetros:

- mnt_sistemas, mnt_tipos_equipo, mnt_contratistas, mnt_personas
- mnt_equipos (con `sitio`: crianza / planta) y mnt_criticidad_evaluaciones
- mnt_parametros, sembrados con los plazos iniciales (D5)
- mnt_diccionario, vacío (se llena en la fase del diccionario)
- mnt_ots, mnt_avisos, mnt_ot_eventos (sus pantallas llegan en PR2)

Sin claves foráneas hacia tablas de otros módulos: el vínculo a estanques es
una referencia blanda (spec §3).

Siembra además un catálogo inicial de sistemas y tipos de equipo. Es solo un
punto de partida editable desde la UI; no condiciona nada.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


revision = "20260929_06"
down_revision = "20260929_05"
branch_labels = None
depends_on = None


# (clave, valor, unidad, descripción)
PARAMETROS = [
    ("plazo_p1_horas", "4", "h", "Plazo objetivo de una OT P1 (alarma). Se congela en cada OT al aceptarla"),
    ("plazo_p2_horas", "24", "h", "Plazo objetivo de una OT P2. Se congela en cada OT al aceptarla"),
    ("plazo_p3_horas", "168", "h", "Plazo objetivo de una OT P3 (7 días). Se congela en cada OT al aceptarla"),
    ("p1_repetir_horas", "2", "h", "Si una alarma P1 pasa este tiempo sin cambio de estado, se repite una vez"),
    ("registro_tarde_horas", "24", "h", "Desfase entre cuándo pasó y cuándo se anotó un cambio a partir del cual se marca como tardío"),
    ("resumen_horas", "8:30", "hh:mm", "Horas del resumen de fallas menores, separadas por coma. Solo sale dentro del horario de cada destinatario"),
    ("inicio_registro", "", "fecha", "Inicio del registro para el aviso de pocos datos (6 meses). Vacío = fecha de la primera OT"),
]

# (nombre, sitio, orden) — sitio None = ambos
SISTEMAS = [
    ("Aireación", "crianza", 10),
    ("Bombeo", "crianza", 20),
    ("Biofiltros", "crianza", 30),
    ("Alimentación", "crianza", 40),
    ("Eléctrico / generación", None, 50),
    ("Frío", "planta", 60),
    ("Proceso", "planta", 70),
]

TIPOS = ["Soplador", "Bomba", "Generador", "Tablero eléctrico", "Motor",
         "Compresor", "Cámara de frío"]


def upgrade() -> None:
    op.create_table(
        "mnt_sistemas",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("nombre", sa.String(80), nullable=False),
        sa.Column("sitio", sa.String(10)),
        sa.Column("orden", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nombre", name="uq_mnt_sistemas_nombre"),
    )
    op.create_table(
        "mnt_tipos_equipo",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("nombre", sa.String(80), nullable=False),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nombre", name="uq_mnt_tipos_equipo_nombre"),
    )
    op.create_table(
        "mnt_contratistas",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("empresa", sa.String(120), nullable=False),
        sa.Column("contacto", sa.String(120)),
        sa.Column("telefono", sa.String(40)),
        sa.Column("especialidad", sa.String(120)),
        sa.Column("notas", sa.Text()),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "mnt_personas",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("rol", sa.String(20), nullable=False, server_default="reportante"),
        sa.Column("sitio", sa.String(10)),
        sa.Column("telegram_user_id", sa.String(40)),
        sa.Column("bot_iniciado", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("alarmas_sitios", sa.String(20)),
        sa.Column("recibe_resumen", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("horario_dias", sa.String(20)),
        sa.Column("horario_desde", sa.Numeric(4, 2)),
        sa.Column("horario_hasta", sa.Numeric(4, 2)),
        sa.Column("planta_username", sa.String(80)),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id", name="uq_mnt_personas_telegram"),
    )
    op.create_table(
        "mnt_equipos",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(20), nullable=False),
        sa.Column("sitio", sa.String(10), nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("tipo_id", sa.BigInteger(), sa.ForeignKey("mnt_tipos_equipo.id")),
        sa.Column("sistema_id", sa.BigInteger(), sa.ForeignKey("mnt_sistemas.id")),
        sa.Column("ubicacion_texto", sa.String(120)),
        sa.Column("ref_ubicacion_tipo", sa.String(30)),
        sa.Column("ref_ubicacion_id", sa.String(40)),
        sa.Column("marca", sa.String(80)),
        sa.Column("modelo", sa.String(80)),
        sa.Column("serie", sa.String(80)),
        sa.Column("potencia_kw", sa.Numeric(8, 2)),
        sa.Column("voltaje", sa.String(20)),
        sa.Column("fecha_instalacion", sa.Date()),
        sa.Column("respaldo_equipo_id", sa.BigInteger(), sa.ForeignKey("mnt_equipos.id")),
        sa.Column("contratista_habitual_id", sa.BigInteger(), sa.ForeignKey("mnt_contratistas.id")),
        sa.Column("foto", sa.LargeBinary()),
        sa.Column("foto_mime", sa.String(40)),
        sa.Column("notas", sa.Text()),
        sa.Column("estado", sa.String(20), nullable=False, server_default="operativo"),
        sa.Column("criticidad", sa.String(1)),
        sa.Column("alta_origen", sa.String(10), nullable=False, server_default="crianza"),
        sa.Column("alta_por", sa.String(120)),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_mnt_equipos_codigo"),
        sa.CheckConstraint("sitio IN ('crianza', 'planta')", name="ck_mnt_equipos_sitio"),
    )
    op.create_index("ix_mnt_equipos_sitio", "mnt_equipos", ["sitio"])

    op.create_table(
        "mnt_criticidad_evaluaciones",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("equipo_id", sa.BigInteger(), sa.ForeignKey("mnt_equipos.id"), nullable=False),
        sa.Column("respuestas", sa.JSON(), nullable=False),
        sa.Column("resultado", sa.String(1), nullable=False),
        sa.Column("tolerancia_horas", sa.Numeric(6, 2)),
        sa.Column("regla_version", sa.String(10), nullable=False),
        sa.Column("evaluado_por", sa.String(120)),
        sa.Column("evaluado_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mnt_criticidad_equipo", "mnt_criticidad_evaluaciones", ["equipo_id"])

    op.create_table(
        "mnt_parametros",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("clave", sa.String(50), nullable=False),
        sa.Column("valor", sa.String(120)),
        sa.Column("unidad", sa.String(20)),
        sa.Column("descripcion", sa.String(250)),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clave", name="uq_mnt_parametros_clave"),
    )
    op.create_table(
        "mnt_diccionario",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dominio", sa.String(10), nullable=False),
        sa.Column("tipo_equipo_id", sa.BigInteger(), sa.ForeignKey("mnt_tipos_equipo.id")),
        sa.Column("etiqueta", sa.String(60), nullable=False),
        sa.Column("sinonimos", sa.Text()),
        sa.Column("orden", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "mnt_ots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tipo", sa.String(12), nullable=False, server_default="correctiva"),
        sa.Column("equipo_id", sa.BigInteger(), sa.ForeignKey("mnt_equipos.id"), nullable=False),
        sa.Column("prioridad", sa.String(2), nullable=False),
        sa.Column("prioridad_motivo", sa.String(200)),
        sa.Column("plazo_horas", sa.Numeric(8, 2)),
        sa.Column("estado", sa.String(20), nullable=False),
        sa.Column("ejecutor_tipo", sa.String(12)),
        sa.Column("tecnico_id", sa.BigInteger(), sa.ForeignKey("mnt_personas.id")),
        sa.Column("contratista_id", sa.BigInteger(), sa.ForeignKey("mnt_contratistas.id")),
        sa.Column("inicio_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("cierre_at", sa.TIMESTAMP()),
        sa.Column("equipo_detenido_desde", sa.TIMESTAMP()),
        sa.Column("equipo_en_servicio_at", sa.TIMESTAMP()),
        sa.Column("causa_texto", sa.Text()),
        sa.Column("accion_texto", sa.Text()),
        sa.Column("causa_codigo", sa.BigInteger(), sa.ForeignKey("mnt_diccionario.id")),
        sa.Column("accion_codigo", sa.BigInteger(), sa.ForeignKey("mnt_diccionario.id")),
        sa.Column("provisorio", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ot_origen_id", sa.BigInteger(), sa.ForeignKey("mnt_ots.id")),
        sa.Column("repuestos_texto", sa.Text()),
        sa.Column("trabajo_realizado", sa.Text()),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mnt_ots_equipo", "mnt_ots", ["equipo_id"])
    op.create_index("ix_mnt_ots_estado", "mnt_ots", ["estado"])

    op.create_table(
        "mnt_avisos",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("equipo_id", sa.BigInteger(), sa.ForeignKey("mnt_equipos.id"), nullable=False),
        sa.Column("origen", sa.String(12), nullable=False),
        sa.Column("reportado_por_id", sa.BigInteger(), sa.ForeignKey("mnt_personas.id")),
        sa.Column("reportante_texto", sa.String(120)),
        sa.Column("detectado_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("condicion", sa.String(20), nullable=False),
        sa.Column("respaldo_entro", sa.String(10)),
        sa.Column("descripcion", sa.Text()),
        sa.Column("sintoma_codigo", sa.BigInteger(), sa.ForeignKey("mnt_diccionario.id")),
        sa.Column("foto", sa.LargeBinary()),
        sa.Column("audio", sa.LargeBinary()),
        sa.Column("prioridad_sugerida", sa.String(2)),
        sa.Column("estado", sa.String(15), nullable=False, server_default="nuevo"),
        sa.Column("ot_id", sa.BigInteger(), sa.ForeignKey("mnt_ots.id")),
        sa.Column("motivo_descarte", sa.String(200)),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mnt_avisos_equipo", "mnt_avisos", ["equipo_id"])
    op.create_index("ix_mnt_avisos_estado", "mnt_avisos", ["estado"])

    op.create_table(
        "mnt_ot_eventos",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ot_id", sa.BigInteger(), sa.ForeignKey("mnt_ots.id"), nullable=False),
        sa.Column("estado_desde", sa.String(20)),
        sa.Column("estado_hasta", sa.String(20), nullable=False),
        sa.Column("ocurrido_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("registrado_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), sa.ForeignKey("mnt_personas.id")),
        sa.Column("actor_texto", sa.String(120)),
        sa.Column("nota", sa.String(300)),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mnt_ot_eventos_ot", "mnt_ot_eventos", ["ot_id"])

    # --- semillas -------------------------------------------------------------
    op.bulk_insert(
        table("mnt_parametros", column("clave", sa.String), column("valor", sa.String),
              column("unidad", sa.String), column("descripcion", sa.String)),
        [{"clave": k, "valor": v, "unidad": u, "descripcion": d} for k, v, u, d in PARAMETROS],
    )
    op.bulk_insert(
        table("mnt_sistemas", column("nombre", sa.String), column("sitio", sa.String),
              column("orden", sa.Integer)),
        [{"nombre": n, "sitio": s, "orden": o} for n, s, o in SISTEMAS],
    )
    op.bulk_insert(
        table("mnt_tipos_equipo", column("nombre", sa.String)),
        [{"nombre": n} for n in TIPOS],
    )


def downgrade() -> None:
    for t in ("mnt_ot_eventos", "mnt_avisos", "mnt_ots", "mnt_diccionario",
              "mnt_parametros", "mnt_criticidad_evaluaciones", "mnt_equipos",
              "mnt_personas", "mnt_contratistas", "mnt_tipos_equipo", "mnt_sistemas"):
        op.drop_table(t)
