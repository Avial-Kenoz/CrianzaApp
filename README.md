# FastApp Etapa 1

## Descripción
Aplicación FastAPI para trazabilidad diaria de peces, siguiendo la especificación de la etapa 1.

## Estructura inicial
- `app/main.py`: Punto de entrada FastAPI
- `app/db/session.py`: Configuración SQLAlchemy
- `app/models/`: Modelos ORM
- `app/api/`: Rutas/API
- `app/schemas/`: Esquemas Pydantic
- `app/core/`: Configuración y utilidades

## Instalación
1. Crea un entorno virtual:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
2. Instala dependencias:
   ```bash
   pip install -r requirements.txt
   ```
3. Copia y configura el archivo `.env`:
   ```bash
   cp .env.example .env
   # Edita las credenciales de la base de datos
   ```

## Ejecución
```bash
uvicorn app.main:app --reload
```

## Migraciones
```bash
alembic init alembic
# Configura alembic.ini y genera migraciones
```

## Próximos pasos
- Definir modelos según especificación
- Crear rutas y lógica de negocio
- Implementar pruebas
