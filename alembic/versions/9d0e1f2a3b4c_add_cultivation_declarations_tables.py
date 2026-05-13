"""add_cultivation_declarations_tables

Revision ID: 9d0e1f2a3b4c
Revises: 8c9d0e1f2a3b
Create Date: 2026-05-04 16:10:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "9d0e1f2a3b4c"
down_revision = "8c9d0e1f2a3b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cultivation_declarations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("folio", sa.String(length=120), nullable=False),
        sa.Column("source_pond_id", sa.BigInteger(), nullable=False),
        sa.Column("cultivation_unit_name", sa.String(length=160), nullable=False),
        sa.Column("manager_name", sa.String(length=160), nullable=False),
        sa.Column("manager_rut", sa.String(length=40), nullable=False),
        sa.Column("manager_phone", sa.String(length=40), nullable=False),
        sa.Column("declaration_date", sa.Date(), nullable=False),
        sa.Column("validity_days", sa.Integer(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=False),
        sa.Column("species_name", sa.String(length=120), nullable=True),
        sa.Column("total_fish", sa.Integer(), nullable=False),
        sa.Column("total_weight_kg", sa.Numeric(12, 3), nullable=True),
        sa.Column("sanitary_report_number", sa.String(length=120), nullable=True),
        sa.Column("sanitary_report_date", sa.Date(), nullable=True),
        sa.Column("sanitary_check_ok", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("drug_check_ok", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("depuration_check_ok", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="sent"),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_by", sa.String(length=160), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["source_pond_id"], ["ponds.id"]),
    )
    op.create_index("ix_cultivation_declarations_folio", "cultivation_declarations", ["folio"], unique=True)
    op.create_index("ix_cultivation_declarations_source_pond_id", "cultivation_declarations", ["source_pond_id"])

    op.create_table(
        "cultivation_declaration_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("declaration_id", sa.BigInteger(), nullable=False),
        sa.Column("fish_id", sa.BigInteger(), nullable=True),
        sa.Column("pit_tag", sa.String(length=120), nullable=False),
        sa.Column("lot_label", sa.String(length=120), nullable=True),
        sa.Column("sex", sa.String(length=20), nullable=True),
        sa.Column("weight_g", sa.Numeric(12, 3), nullable=True),
        sa.Column("depuration_days", sa.Integer(), nullable=True),
        sa.Column("has_drug_use", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("withdrawal_end_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["declaration_id"], ["cultivation_declarations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fish_id"], ["fish.id"]),
    )
    op.create_index("ix_cultivation_declaration_items_declaration_id", "cultivation_declaration_items", ["declaration_id"])
    op.create_index("ix_cultivation_declaration_items_fish_id", "cultivation_declaration_items", ["fish_id"])


def downgrade():
    op.drop_index("ix_cultivation_declaration_items_fish_id", table_name="cultivation_declaration_items")
    op.drop_index("ix_cultivation_declaration_items_declaration_id", table_name="cultivation_declaration_items")
    op.drop_table("cultivation_declaration_items")

    op.drop_index("ix_cultivation_declarations_source_pond_id", table_name="cultivation_declarations")
    op.drop_index("ix_cultivation_declarations_folio", table_name="cultivation_declarations")
    op.drop_table("cultivation_declarations")
