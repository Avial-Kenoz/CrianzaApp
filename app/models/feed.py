from sqlalchemy import Column, BigInteger, String, Integer, Numeric, Float, TIMESTAMP, ForeignKey, DateTime, Text, Enum as SQLEnum, UniqueConstraint
from app.db.session import Base
import enum

# ============================================================================
# ENUMS
# ============================================================================

class FeedReceiptStatus(str, enum.Enum):
    draft = "draft"
    confirmed = "confirmed"
    canceled = "canceled"

class FeedMovementType(str, enum.Enum):
    entry = "entry"
    reserve = "reserve"
    execute = "execute"
    release = "release"
    adjustment = "adjustment"

class AllocationMethod(str, enum.Enum):
    biomass = "biomass"
    fish_count = "fish_count"


def enum_as_varchar(enum_cls):
    return SQLEnum(
        enum_cls,
        native_enum=False,
        values_callable=lambda members: [member.value for member in members],
    )

# ============================================================================
# MODELS
# ============================================================================

class FeedType(Base):
    """Catalogo de tipos de alimento (ej: Alimento Estandar, Premium, etc)"""
    __tablename__ = "feed_types"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False, unique=True)
    active = Column(Integer, default=1, nullable=False)
    created_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, nullable=True)


class FeedReceiptHeader(Base):
    """Cabecera de recepcion por guia de despacho"""
    __tablename__ = "feed_receipt_headers"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    guia_despacho = Column(String(120), nullable=False, unique=True, index=True)
    supplier_name = Column(String(200), nullable=True)
    fecha_ingreso = Column(DateTime, nullable=False)
    status = Column(enum_as_varchar(FeedReceiptStatus), default=FeedReceiptStatus.draft, nullable=False)
    created_by = Column(String(120), nullable=True)
    created_at = Column(TIMESTAMP, nullable=True)
    confirmed_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, nullable=True)


class FeedReceiptLine(Base):
    """Detalle de productos por guia (1..N lineas por cabecera)"""
    __tablename__ = "feed_receipt_lines"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    feed_receipt_id = Column(BigInteger, ForeignKey("feed_receipt_headers.id"), nullable=False)
    feed_type_id = Column(BigInteger, ForeignKey("feed_types.id"), nullable=False)
    quantity_kg = Column(Numeric(14, 3), nullable=False)
    bag_weight_kg = Column(Integer, nullable=False)  # 20 o 25
    quantity_bags = Column(Integer, nullable=False)  # Derivado
    supplier_lot = Column(String(120), nullable=True)
    expiration_date = Column(DateTime, nullable=True)
    created_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, nullable=True)


class FeedInventoryAdjustment(Base):
    """Cuadres manuales de inventario"""
    __tablename__ = "feed_inventory_adjustments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    feed_type_id = Column(BigInteger, ForeignKey("feed_types.id"), nullable=False)
    stock_calculated_bags = Column(Integer, nullable=False)
    stock_real_bags = Column(Integer, nullable=False)
    difference_bags = Column(Integer, nullable=False)  # Derivado
    comment = Column(Text, nullable=False)
    created_by = Column(String(120), nullable=True)
    created_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, nullable=True)


class FeedStockLedger(Base):
    """Libro mayor de movimientos de stock (auditoria y conciliacion)"""
    __tablename__ = "feed_stock_ledger"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    feed_type_id = Column(BigInteger, ForeignKey("feed_types.id"), nullable=False)
    movement_type = Column(enum_as_varchar(FeedMovementType), nullable=False)
    bags_delta = Column(Integer, nullable=False)
    kg_delta = Column(Numeric(14, 3), nullable=False)
    source_table = Column(String(120), nullable=True)  # ej: feed_receipt_headers, feed_inventory_adjustments
    source_id = Column(BigInteger, nullable=True)
    created_at = Column(TIMESTAMP, nullable=True)
    created_by = Column(String(120), nullable=True)


# ============================================================================
# FUTURE MODELS (Phase 2+)
# ============================================================================

class FeedProgram(Base):
    """Cabecera de planificacion semanal (Phase 2)"""
    __tablename__ = "feed_programs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    program_code = Column(String(120), nullable=False, unique=True)
    week_start_date = Column(DateTime, nullable=False)
    week_end_date = Column(DateTime, nullable=False)
    status = Column(String(50), default="open", nullable=False)
    closed_reason = Column(String(50), nullable=True)  # depleted, superseded
    created_by = Column(String(120), nullable=True)
    created_at = Column(TIMESTAMP, nullable=True)
    closed_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, nullable=True)


class FeedProgramLine(Base):
    """Detalle planificado por estanque y tipo de alimento"""
    __tablename__ = "feed_program_lines"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    feed_program_id = Column(BigInteger, ForeignKey("feed_programs.id"), nullable=False)
    pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=False)
    feed_type_id = Column(BigInteger, ForeignKey("feed_types.id"), nullable=False)
    planned_bags = Column(Integer, nullable=False, default=0)
    planned_kg = Column(Numeric(14, 3), nullable=False, default=0)
    executed_bags = Column(Integer, nullable=False, default=0)
    executed_kg = Column(Numeric(14, 3), nullable=False, default=0)
    remaining_bags = Column(Integer, nullable=False, default=0)
    remaining_kg = Column(Numeric(14, 3), nullable=False, default=0)
    created_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, nullable=True)


class FeedExecutionEvent(Base):
    """Evento de confirmacion de ejecucion (permite multiples confirmaciones parciales)"""
    __tablename__ = "feed_execution_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    feed_program_id = Column(BigInteger, ForeignKey("feed_programs.id"), nullable=False)
    feed_program_line_id = Column(BigInteger, ForeignKey("feed_program_lines.id"), nullable=True)
    pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=False)
    feed_type_id = Column(BigInteger, ForeignKey("feed_types.id"), nullable=False)
    confirmed_bags = Column(Integer, nullable=False)
    confirmed_kg = Column(Numeric(14, 3), nullable=False, default=0)
    is_unplanned = Column(Integer, default=0, nullable=False)  # 0=planned, 1=unplanned
    confirmed_at = Column(TIMESTAMP, nullable=True)
    confirmed_by = Column(String(120), nullable=True)
    note = Column(Text, nullable=True)


class FeedExecutionLotAllocations(Base):
    """Asignacion de consumo confirmado por lote dentro de cada estanque (Phase 3+)"""
    __tablename__ = "feed_execution_lot_allocations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    feed_execution_event_id = Column(BigInteger, nullable=False)  # Will add FK in Phase 3
    lot_id = Column(BigInteger, ForeignKey("lots.id"), nullable=False)
    allocated_bags = Column(Integer, nullable=False)
    allocated_kg = Column(Numeric(14, 3), nullable=False)
    allocation_method = Column(enum_as_varchar(AllocationMethod), nullable=False)
    biomass_estimated_kg = Column(Numeric(14, 3), nullable=True)
    fish_count_estimated = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, nullable=True)


class AccountingOutboxEvent(Base):
    """Outbox para integracion futura con PlantaApp Contabilidad"""
    __tablename__ = "accounting_outbox_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_type = Column(String(120), nullable=False)
    payload_json = Column(Text, nullable=False)
    status = Column(String(50), default="pending", nullable=False)
    retry_count = Column(Integer, default=0, nullable=False)
    created_at = Column(TIMESTAMP, nullable=True)
    sent_at = Column(TIMESTAMP, nullable=True)
    last_error = Column(Text, nullable=True)


# ============================================================================
# LEGACY IMPORT — consumo historico desde planillas Excel 2023-2026
# ============================================================================

class FeedMonthlyConsumptionLegacy(Base):
    """Consumo mensual por estanque importado desde planillas Excel historicas."""
    __tablename__ = "feed_monthly_consumption_legacy"

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    year           = Column(Integer, nullable=False)
    month          = Column(Integer, nullable=False)
    pond_code      = Column(String(10), nullable=False)
    pond_id        = Column(BigInteger, ForeignKey("ponds.id"), nullable=True, index=True)
    feed_type_raw  = Column(String(120), nullable=False)
    feed_type_key  = Column(String(60), nullable=False)
    consumed_kg    = Column(Float, nullable=False)
    source_file    = Column(String(200), nullable=False)

    __table_args__ = (
        UniqueConstraint("year", "month", "pond_code", "feed_type_key",
                         name="uq_feed_monthly_legacy"),
    )


class FeedMonthlyStockLegacy(Base):
    """Resumen mensual de stock (inicial, ingresos, consumido, final) por tipo de alimento."""
    __tablename__ = "feed_monthly_stock_legacy"

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    year          = Column(Integer, nullable=False)
    month         = Column(Integer, nullable=False)
    feed_type_key = Column(String(60), nullable=False)
    feed_type_raw = Column(String(120), nullable=False)
    initial_kg    = Column(Float, nullable=True)
    receipts_kg   = Column(Float, nullable=True)
    consumed_kg   = Column(Float, nullable=True)
    final_kg      = Column(Float, nullable=True)
    source_file   = Column(String(200), nullable=False)

    __table_args__ = (
        UniqueConstraint("year", "month", "feed_type_key",
                         name="uq_feed_monthly_stock_legacy"),
    )
