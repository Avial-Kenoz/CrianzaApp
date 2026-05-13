-- FASTAPP ETAPA 1 - ETL SQL TEMPLATE
-- Requiere:
-- 1) Base legacy restaurada (schema public)
-- 2) Base FastApp con esquema creado (fastapp_etapa1_schema.sql)
-- 3) FDW o dblink entre ambas bases, o ejecucion en la misma base temporal.

BEGIN;

-- 0) Catalogos
INSERT INTO species (legacy_id, name, internal_id, created_at, updated_at)
SELECT id, name, internal_id, created_at, updated_at
FROM legacy.public.species;

INSERT INTO cultivation_units (legacy_id, name, created_at, updated_at)
SELECT id, name, created_at, updated_at
FROM legacy.public.cultivation_units;

INSERT INTO pond_types (legacy_id, name, created_at, updated_at)
SELECT id, name, created_at, updated_at
FROM legacy.public.pond_types;

INSERT INTO users (legacy_id, email, name, lastname, rut, active, created_at, updated_at)
SELECT id,
       COALESCE(NULLIF(email, ''), CONCAT('legacy_', id, '@fastapp.local')),
       name,
       lastname,
       rut,
       TRUE,
       created_at,
       updated_at
FROM legacy.public.users;

INSERT INTO roles (legacy_id, name, resource_type, resource_id, created_at, updated_at)
SELECT id, name, resource_type, resource_id, created_at, updated_at
FROM legacy.public.roles;

INSERT INTO users_roles (user_id, role_id)
SELECT u.id, r.id
FROM legacy.public.users_roles ur
JOIN users u ON u.legacy_id = ur.user_id
JOIN roles r ON r.legacy_id = ur.role_id;

-- 1) Estructura crianza
INSERT INTO lots (legacy_id, species_id, name, internal_id, origin, hatch_year, creation_time, creation_date, national, created_at, updated_at)
SELECT l.id, s.id, l.name, l.internal_id, l.origin, l.hatch_year, l.creation_time, l.creation_date, l.national, l.created_at, l.updated_at
FROM legacy.public.lots l
JOIN species s ON s.legacy_id = l.species_id;

INSERT INTO ponds (legacy_id, name, internal_id, code, cultivation_unit_id, pond_type_id, lot_id, volume, depuration, state, biomass, avg_weight, created_at, updated_at)
SELECT p.id,
       p.name,
       p.internal_id,
       p.code,
       cu.id,
       pt.id,
       l.id,
       p.volume,
       p.depuration,
       p.state,
       p.biomass,
       p.avg_weight,
       p.created_at,
       p.updated_at
FROM legacy.public.ponds p
LEFT JOIN cultivation_units cu ON cu.legacy_id = p.cultivation_unit_id
LEFT JOIN pond_types pt ON pt.legacy_id = p.pond_type_id
LEFT JOIN lots l ON l.legacy_id = p.lot_id;

INSERT INTO fish (legacy_id, internal_id, lot_id, sex, state, registration_time, devious_time, date_of_death, depuration_start_time, mortality_id, created_at, updated_at)
SELECT f.id,
       f.internal_id,
       l.id,
       f.sex,
       f.state,
       f.registration_time,
       f.devious_time,
       f.date_of_death,
       f.dupuration_start_time,
       f.mortality_id,
       f.created_at,
       f.updated_at
FROM legacy.public.fish f
JOIN lots l ON l.legacy_id = f.lot_id;

INSERT INTO ponds_movements (legacy_id, source_pond_id, destiny_pond_id, fish_id, lot_id, fish_quantity, movement_reason, movement_time, folio, created_at, updated_at)
SELECT pm.id,
       psrc.id,
       pdst.id,
       fi.id,
       lo.id,
       pm.fish_quantity,
       pm.movement_reason,
       pm.movement_time,
       pm.folio,
       pm.created_at,
       pm.updated_at
FROM legacy.public.ponds_movements pm
LEFT JOIN ponds psrc ON psrc.legacy_id = pm.source_pond_id
LEFT JOIN ponds pdst ON pdst.legacy_id = pm.destiny_pond_id
LEFT JOIN fish fi ON fi.legacy_id = pm.fish_id
LEFT JOIN lots lo ON lo.legacy_id = pm.lot_id;

INSERT INTO fish_female_samplings (legacy_id, user_id, fish_id, length, weight, oocyte_size, color, gonad_size, flavor, development_state, harvest_date, registry_time, diameter, agglomeration, turgor, fat, created_at, updated_at)
SELECT ffs.id,
       u.id,
       f.id,
       ffs.length,
       ffs.weight,
       ffs.oocyte_size,
       ffs.color,
       ffs.gonad_size,
       ffs.flavor,
       ffs.development_state,
       ffs.harvest_date,
       ffs.registry_time,
       ffs.diameter,
       ffs.agglomeration,
       ffs.turgor,
       ffs.fat,
       ffs.created_at,
       ffs.updated_at
FROM legacy.public.fish_female_samplings ffs
LEFT JOIN users u ON u.legacy_id = ffs.user_id
JOIN fish f ON f.legacy_id = ffs.fish_id;

INSERT INTO mortality_reports (legacy_id, comment, registry_time, breeding_user_id, supervisor_user_id, general_user_id, supervisor_validation, general_validation, supervisor_validation_time, general_validation_time, created_at, updated_at)
SELECT mr.id,
       mr.comment,
       mr.registry_time,
       ub.id,
       us.id,
       ug.id,
       mr.supervisor_validation,
       mr.general_validation,
       mr.supervisor_validation_time,
       mr.general_validation_time,
       mr.created_at,
       mr.updated_at
FROM legacy.public.mortality_reports mr
LEFT JOIN users ub ON ub.legacy_id = mr.breeding_user_id
LEFT JOIN users us ON us.legacy_id = mr.supervisor_user_id
LEFT JOIN users ug ON ug.legacy_id = mr.general_user_id;

INSERT INTO marked_mortality_registries (legacy_id, mortality_report_id, ponds_movement_id, comment, registry_time, supervisor_validation, general_validation, created_at, updated_at)
SELECT mmr.id,
       mr.id,
       pm.id,
       mmr.comment,
       mmr.registry_time,
       mmr.supervisor_validation,
       mmr.general_validation,
       mmr.created_at,
       mmr.updated_at
FROM legacy.public.marked_mortality_registries mmr
JOIN mortality_reports mr ON mr.legacy_id = mmr.mortality_report_id
LEFT JOIN ponds_movements pm ON pm.legacy_id = mmr.ponds_movement_id;

INSERT INTO unmarked_mortality_registries (legacy_id, mortality_report_id, ponds_movement_id, comment, registry_time, supervisor_validation, general_validation, created_at, updated_at)
SELECT umr.id,
       mr.id,
       pm.id,
       umr.comment,
       umr.registry_time,
       umr.supervisor_validation,
       umr.general_validation,
       umr.created_at,
       umr.updated_at
FROM legacy.public.unmarked_mortality_registries umr
JOIN mortality_reports mr ON mr.legacy_id = umr.mortality_report_id
LEFT JOIN ponds_movements pm ON pm.legacy_id = umr.ponds_movement_id;

INSERT INTO depuration_periodic_samplings (legacy_id, fish_id, pond_id, development_state, width, heigh, weight, harvest_date, color, gonad_dimension, oocyte_size, flavor, created_at, updated_at)
SELECT dps.id,
       f.id,
       p.id,
       dps.development_state,
       dps.width,
       dps.heigh,
       dps.weight,
       dps.harvest_date,
       dps.color,
       dps.gonad_dimension,
       dps.oocyte_size,
       dps.flavor,
       dps.created_at,
       dps.updated_at
FROM legacy.public.depuration_periodic_samplings dps
LEFT JOIN fish f ON f.legacy_id = dps.fish_id
LEFT JOIN ponds p ON p.legacy_id = dps.pond_id;

INSERT INTO transports (legacy_id, sheet_number, delivery_date, chofer, phone_number, license_plate, observations, created_at, updated_at)
SELECT id, sheet_number, delivery_date, chofer, phone_number, license_plate, observations, created_at, updated_at
FROM legacy.public.transports;

-- 2) Construccion de eventos de trazabilidad
INSERT INTO fish_traceability_events (fish_id, event_type, event_time, source_table, source_id, payload)
SELECT pm.fish_id,
       'movement',
       COALESCE(pm.movement_time, pm.created_at),
       'ponds_movements',
       pm.id,
       jsonb_build_object(
         'source_pond_id', pm.source_pond_id,
         'destiny_pond_id', pm.destiny_pond_id,
         'fish_quantity', pm.fish_quantity,
         'movement_reason', pm.movement_reason,
         'lot_id', pm.lot_id
       )
FROM ponds_movements pm
WHERE pm.fish_id IS NOT NULL;

INSERT INTO fish_traceability_events (fish_id, event_type, event_time, source_table, source_id, payload)
SELECT ffs.fish_id,
       'female_sampling',
       COALESCE(ffs.registry_time, ffs.created_at),
       'fish_female_samplings',
       ffs.id,
       to_jsonb(ffs.*)
FROM fish_female_samplings ffs;

INSERT INTO fish_traceability_events (fish_id, event_type, event_time, source_table, source_id, payload)
SELECT dps.fish_id,
       'depuration_sampling',
       COALESCE(dps.created_at, NOW()),
       'depuration_periodic_samplings',
       dps.id,
       to_jsonb(dps.*)
FROM depuration_periodic_samplings dps
WHERE dps.fish_id IS NOT NULL;

COMMIT;

-- 3) Queries de control de calidad sugeridas
-- SELECT COUNT(*) FROM legacy.public.fish;
-- SELECT COUNT(*) FROM fish;
-- SELECT COUNT(*) FROM ponds_movements WHERE lot_id IS NULL;
-- SELECT COUNT(*) FROM fish_female_samplings WHERE fish_id IS NULL;
-- SELECT COUNT(*) FROM depuration_periodic_samplings WHERE fish_id IS NULL;
