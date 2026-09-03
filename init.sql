CREATE TABLE airports (
    id SERIAL PRIMARY KEY,
    icao VARCHAR(10) UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE metars (
    id SERIAL PRIMARY KEY,
    airport_id INTEGER NOT NULL REFERENCES airports(id) ON DELETE CASCADE,
    validade_inicial TIMESTAMP NOT NULL,
    vento_direcao VARCHAR(10),
    vento_velocidade INTEGER,
    vento_rajada INTEGER,
    visibilidade INTEGER,
    condicao VARCHAR(100),
    temperatura DECIMAL(5,2),
    ponto_orvalho DECIMAL(5,2),
    qnh DECIMAL(6,1),
    teto INTEGER,
    raw_metar TEXT,
    recebimento TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(airport_id, validade_inicial)
);

CREATE TABLE tafs (
    id SERIAL PRIMARY KEY,
    airport_id INTEGER NOT NULL REFERENCES airports(id) ON DELETE CASCADE,
    confeccao_data TIMESTAMP,
    tipo_grupo VARCHAR(10),
    data_hora TIMESTAMP NOT NULL,
    vento_direcao VARCHAR(10),
    vento_velocidade INTEGER,
    vento_rajada INTEGER,
    visibilidade INTEGER,
    condicao VARCHAR(100),
    temperatura_min DECIMAL(5,2),
    temperatura_max DECIMAL(5,2),
    teto INTEGER,
    cb BOOLEAN DEFAULT FALSE,
    tcu BOOLEAN DEFAULT FALSE,
    raw_taf TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(airport_id, confeccao_data, tipo_grupo, data_hora)
);

CREATE INDEX idx_metars_airport_id ON metars(airport_id);
CREATE INDEX idx_metars_validade ON metars(validade_inicial);
CREATE INDEX idx_metars_airport_validade ON metars(airport_id, validade_inicial);
CREATE INDEX idx_tafs_airport_id ON tafs(airport_id);
CREATE INDEX idx_tafs_data_hora ON tafs(data_hora);
