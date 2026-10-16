"""Tests de la dieta teórica Aller (funciones puras, sin BD).

    .venv\\Scripts\\python.exe -m unittest app.services.test_feed_ration -v
"""
import unittest

from app.services import feed_ration as fr


class TablaPorAlimento(unittest.TestCase):
    def test_nombres_del_catalogo(self):
        self.assertEqual(fr.table_for_feed("Progress EX 8.0"), "progress")
        self.assertEqual(fr.table_for_feed("Reproductor EX 6.0"), "reproductor")
        self.assertEqual(fr.table_for_feed("Thalassa 1.3/2.0"), "thalassa")
        self.assertEqual(fr.table_for_feed("Infa 0.2"), "infa")
        self.assertIsNone(fr.table_for_feed("Otro alimento"))
        self.assertIsNone(fr.table_for_feed(None))


class TasaAller(unittest.TestCase):
    def test_valor_de_la_ficha_en_columna_exacta(self):
        r = fr.aller_rate("progress", 5000, 18)
        self.assertAlmostEqual(r["pct"], 0.62)
        self.assertEqual(r["calibre"], "8 mm")
        self.assertEqual(r["avisos"], [])

    def test_interpola_temperatura(self):
        # 4000-7000 g: 16 °C = 0,50 y 18 °C = 0,62 -> 17 °C = 0,56
        self.assertAlmostEqual(fr.aller_rate("progress", 5000, 17)["pct"], 0.56)

    def test_borde_de_banda_es_cerrado_abajo(self):
        self.assertEqual(fr.aller_rate("progress", 100, 18)["banda"], "100-200 g")

    def test_reproductor_es_la_mitad_que_progress(self):
        self.assertAlmostEqual(fr.aller_rate("reproductor", 5000, 18)["pct"], 0.33)

    def test_pez_sobre_la_tabla_usa_ultima_banda_y_avisa(self):
        r = fr.aller_rate("reproductor", 15000, 18)
        self.assertAlmostEqual(r["pct"], 0.33)
        self.assertTrue(any("extrapolado" in a for a in r["avisos"]))

    def test_temperatura_fuera_de_rango_usa_extremo_y_avisa(self):
        r = fr.aller_rate("progress", 5000, 8)
        self.assertAlmostEqual(r["pct"], 0.06)
        self.assertTrue(any("temperatura" in a for a in r["avisos"]))

    def test_sin_datos_no_inventa(self):
        self.assertIsNone(fr.aller_rate("progress", None, 18))
        self.assertIsNone(fr.aller_rate("progress", 5000, None))
        self.assertIsNone(fr.aller_rate(None, 5000, 18))


class RacionTeorica(unittest.TestCase):
    def test_biomasa_por_porcentaje(self):
        r = fr.theoretical_kg_day(9000, 5000, 18, "reproductor")
        self.assertAlmostEqual(r["kg_day"], 29.7)

    def test_sin_biomasa(self):
        self.assertIsNone(fr.theoretical_kg_day(0, 5000, 18, "progress"))


if __name__ == "__main__":
    unittest.main()
