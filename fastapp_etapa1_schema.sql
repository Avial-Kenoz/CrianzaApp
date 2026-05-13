BEGIN;

CREATE TABLE species (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  name VARCHAR(120) NOT NULL,
  internal_id VARCHAR(120),
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE cultivation_units (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  name VARCHAR(120) NOT NULL,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE pond_types (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  name VARCHAR(120) NOT NULL,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE users (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  email VARCHAR(190) NOT NULL UNIQUE,
  name VARCHAR(120),
  lastname VARCHAR(120),
  rut VARCHAR(30),
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE roles (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  name VARCHAR(120) NOT NULL,
  resource_type VARCHAR(120),
  resource_id BIGINT,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE users_roles (
  user_id BIGINT NOT NULL REFERENCES users(id),
  role_id BIGINT NOT NULL REFERENCES roles(id),
  PRIMARY KEY (user_id, role_id)
);

CREATE TABLE lots (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  species_id BIGINT NOT NULL REFERENCES species(id),
  name VARCHAR(120) NOT NULL,
  internal_id VARCHAR(120),
  origin VARCHAR(120),
  hatch_year INT,
  creation_time TIMESTAMP,
  creation_date TIMESTAMP,
  national BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE ponds (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  name VARCHAR(120) NOT NULL,
  internal_id VARCHAR(120),
  code VARCHAR(120),
  cultivation_unit_id BIGINT REFERENCES cultivation_units(id),
  pond_type_id BIGINT REFERENCES pond_types(id),
  lot_id BIGINT REFERENCES lots(id),
  volume INT DEFAULT 0,
  depuration BOOLEAN DEFAULT FALSE,
  state VARCHAR(50) DEFAULT 'success',
  biomass NUMERIC(14,3),
  avg_weight NUMERIC(14,3),
  tagged_count INT DEFAULT 0,
  unregistered_count INT DEFAULT 0,
  n_fish_cached INT DEFAULT 0,
  active_lots_count INT DEFAULT 0,
  active_lot_ids JSONB DEFAULT '[]'::jsonb,
  unregistered_lot_ids JSONB DEFAULT '[]'::jsonb,
  unregistered_lot_conflict BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE fish (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  internal_id VARCHAR(120) NOT NULL,
  lot_id BIGINT NOT NULL REFERENCES lots(id),
  sex VARCHAR(20),
  state VARCHAR(30) DEFAULT 'alive',
  registration_time TIMESTAMP,
  devious_time TIMESTAMP,
  date_of_death TIMESTAMP,
  depuration_start_time TIMESTAMP,
  mortality_id VARCHAR(120),
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE ponds_movements (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  source_pond_id BIGINT REFERENCES ponds(id),
  destiny_pond_id BIGINT REFERENCES ponds(id),
  fish_id BIGINT REFERENCES fish(id),
  lot_id BIGINT REFERENCES lots(id),
  fish_quantity INT NOT NULL,
  movement_reason VARCHAR(50) NOT NULL,
  movement_time TIMESTAMP NOT NULL,
  folio INT,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE fish_female_samplings (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  user_id BIGINT REFERENCES users(id),
  fish_id BIGINT NOT NULL REFERENCES fish(id),
  length NUMERIC(10,3),
  weight NUMERIC(10,3),
  oocyte_size NUMERIC(10,3),
  color VARCHAR(80),
  gonad_size NUMERIC(10,3),
  flavor VARCHAR(80),
  development_state VARCHAR(80),
  harvest_date TIMESTAMP,
  registry_time TIMESTAMP,
  diameter NUMERIC(10,3),
  agglomeration INT,
  turgor INT,
  fat INT,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE mortality_reports (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  comment TEXT,
  registry_time TIMESTAMP,
  breeding_user_id BIGINT REFERENCES users(id),
  supervisor_user_id BIGINT REFERENCES users(id),
  general_user_id BIGINT REFERENCES users(id),
  supervisor_validation BOOLEAN DEFAULT FALSE,
  general_validation BOOLEAN DEFAULT FALSE,
  supervisor_validation_time TIMESTAMP,
  general_validation_time TIMESTAMP,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE marked_mortality_registries (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  mortality_report_id BIGINT NOT NULL REFERENCES mortality_reports(id),
  ponds_movement_id BIGINT REFERENCES ponds_movements(id),
  comment TEXT,
  registry_time TIMESTAMP,
  supervisor_validation BOOLEAN DEFAULT FALSE,
  general_validation BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE unmarked_mortality_registries (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  mortality_report_id BIGINT NOT NULL REFERENCES mortality_reports(id),
  ponds_movement_id BIGINT REFERENCES ponds_movements(id),
  comment TEXT,
  registry_time TIMESTAMP,
  supervisor_validation BOOLEAN DEFAULT FALSE,
  general_validation BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE depuration_periodic_samplings (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  fish_id BIGINT REFERENCES fish(id),
  pond_id BIGINT REFERENCES ponds(id),
  development_state VARCHAR(80),
  width NUMERIC(10,3),
  heigh NUMERIC(10,3),
  weight NUMERIC(10,3),
  harvest_date DATE,
  color VARCHAR(80),
  gonad_dimension NUMERIC(10,3),
  oocyte_size NUMERIC(10,3),
  flavor VARCHAR(80),
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE transports (
  id BIGSERIAL PRIMARY KEY,
  legacy_id BIGINT UNIQUE,
  sheet_number INT,
  delivery_date TIMESTAMP,
  chofer VARCHAR(120),
  phone_number BIGINT,
  license_plate VARCHAR(40),
  observations TEXT,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE fish_traceability_events (
  id BIGSERIAL PRIMARY KEY,
  fish_id BIGINT NOT NULL REFERENCES fish(id),
  event_type VARCHAR(40) NOT NULL,
  event_time TIMESTAMP NOT NULL,
  source_table VARCHAR(80) NOT NULL,
  source_id BIGINT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_species_internal_id ON species(internal_id);
CREATE INDEX idx_cultivation_units_name ON cultivation_units(name);
CREATE INDEX idx_pond_types_name ON pond_types(name);
CREATE INDEX idx_lots_internal_id ON lots(internal_id);
CREATE INDEX idx_lots_species_id ON lots(species_id);
CREATE INDEX idx_ponds_internal_id ON ponds(internal_id);
CREATE INDEX idx_ponds_lot_id ON ponds(lot_id);
CREATE INDEX idx_ponds_cultivation_unit_id ON ponds(cultivation_unit_id);
CREATE INDEX idx_fish_internal_id ON fish(internal_id);
CREATE INDEX idx_fish_state ON fish(state);
CREATE INDEX idx_fish_lot_id ON fish(lot_id);
CREATE INDEX idx_movements_fish_time ON ponds_movements(fish_id, movement_time DESC);
CREATE INDEX idx_movements_lot_time ON ponds_movements(lot_id, movement_time DESC);
CREATE INDEX idx_movements_source_time ON ponds_movements(source_pond_id, movement_time DESC);
CREATE INDEX idx_movements_destiny_time ON ponds_movements(destiny_pond_id, movement_time DESC);
CREATE INDEX idx_female_sampling_fish_time ON fish_female_samplings(fish_id, registry_time DESC);
CREATE INDEX idx_mortality_reports_time ON mortality_reports(registry_time DESC);
CREATE INDEX idx_marked_mortality_report_id ON marked_mortality_registries(mortality_report_id);
CREATE INDEX idx_unmarked_mortality_report_id ON unmarked_mortality_registries(mortality_report_id);
CREATE INDEX idx_depuration_fish_time ON depuration_periodic_samplings(fish_id, created_at DESC);
CREATE INDEX idx_depuration_pond_time ON depuration_periodic_samplings(pond_id, created_at DESC);
CREATE INDEX idx_traceability_fish_time ON fish_traceability_events(fish_id, event_time DESC);

COMMIT;
