"""Módulo de Mantenimiento de Maquinaria (ver especificacion_mantenimiento_v1.md).

Núcleo único para los dos sitios (Crianza y Planta): el encargado de
mantenimiento es el mismo, así que las tablas, el bot, las OT y los indicadores
viven solo acá. PlantaApp es un satélite que habla con este módulo por una API
firmada (PR3P).

Capas, de abajo hacia arriba:
  models.py   tablas mnt_* (sin FK a tablas de otros módulos)
  rules.py    reglas puras (criticidad…), sin BD ni FastAPI
  service.py  casos de uso; los llaman la web, la API de Planta y el bot
  router.py   UI web del encargado bajo /views/ui/mantenimiento
"""
