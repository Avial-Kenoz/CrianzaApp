-- Verificar estructura de C4 y sus hijos
SELECT 
    id, 
    name, 
    parent_pond_id,
    active_lot_ids,
    lot_id
FROM ponds 
WHERE name LIKE '%C4%' OR name LIKE '%Central 4%' 
ORDER BY id, parent_pond_id;

-- Verificar todos los padres (parent_pond_id IS NULL)
SELECT 
    id, 
    name, 
    parent_pond_id,
    active_lot_ids,
    cultivation_unit_id
FROM ponds 
WHERE parent_pond_id IS NULL 
ORDER BY id;

-- Verificar qué estanques aparecerían en la query
SELECT 
    p.id, 
    p.name, 
    p.parent_pond_id,
    COALESCE(cu.id, 999999) as cu_id,
    cu.name as cu_name
FROM ponds p
LEFT JOIN cultivation_units cu ON p.cultivation_unit_id = cu.id
WHERE p.parent_pond_id IS NULL
ORDER BY COALESCE(cu.id, 999999), p.id;
