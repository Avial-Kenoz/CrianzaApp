"""Pruebas de rules.py. Solo stdlib (el .venv de CrianzaApp no trae pytest):

    .venv\\Scripts\\python.exe -m unittest app.maintenance.test_rules -v
"""
import unittest

from app.maintenance import rules


def _r(impacto, respaldo, reposicion="c", seguridad="no"):
    return {"impacto": impacto, "respaldo": respaldo,
            "reposicion": reposicion, "seguridad_ambiente": seguridad}


class CriticidadTest(unittest.TestCase):
    def test_matriz_base(self):
        esperado = {
            ("a", "a"): "A", ("a", "b"): "A", ("a", "c"): "B",
            ("b", "a"): "A", ("b", "b"): "B", ("b", "c"): "C",
            ("c", "a"): "B", ("c", "b"): "C", ("c", "c"): "C",
        }
        for (imp, resp), crit in esperado.items():
            with self.subTest(impacto=imp, respaldo=resp):
                self.assertEqual(rules.calcular_criticidad(_r(imp, resp))[0], crit)

    def test_reposicion_larga_sube_un_nivel(self):
        self.assertEqual(rules.calcular_criticidad(_r("c", "c", "a"))[0], "B")
        self.assertEqual(rules.calcular_criticidad(_r("a", "c", "a"))[0], "A")
        # A no tiene a dónde subir
        self.assertEqual(rules.calcular_criticidad(_r("a", "a", "a"))[0], "A")
        # reposición media no sube
        self.assertEqual(rules.calcular_criticidad(_r("c", "c", "b"))[0], "C")

    def test_seguridad_fuerza_a(self):
        self.assertEqual(rules.calcular_criticidad(_r("c", "c", "c", "si"))[0], "A")

    def test_tolerancia_sale_del_impacto(self):
        self.assertEqual(rules.calcular_criticidad(_r("a", "c"))[1], 2.0)
        self.assertEqual(rules.calcular_criticidad(_r("b", "c"))[1], 24.0)
        self.assertIsNone(rules.calcular_criticidad(_r("c", "c"))[1])

    def test_respuesta_faltante_es_error_no_default(self):
        incompleta = _r("a", "a")
        del incompleta["reposicion"]
        with self.assertRaises(rules.RespuestasInvalidas):
            rules.calcular_criticidad(incompleta)
        with self.assertRaises(rules.RespuestasInvalidas):
            rules.calcular_criticidad(_r("x", "a"))

    def test_normaliza_mayusculas_y_espacios(self):
        self.assertEqual(rules.calcular_criticidad(_r(" A ", "A", "C", "NO"))[0], "A")


class CodigoTest(unittest.TestCase):
    def test_primero(self):
        self.assertEqual(rules.siguiente_codigo([]), "EQ-001")

    def test_sigue_desde_el_mayor_e_ignora_raros(self):
        self.assertEqual(rules.siguiente_codigo(["EQ-001", "EQ-007", "X-99", "EQ-abc"]), "EQ-008")

    def test_pasa_de_999(self):
        self.assertEqual(rules.siguiente_codigo(["EQ-999"]), "EQ-1000")


class HoraTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(rules.parse_hora("8:30"), 8.5)
        self.assertEqual(rules.parse_hora("8,5"), 8.5)
        self.assertEqual(rules.parse_hora("17"), 17.0)
        self.assertIsNone(rules.parse_hora(""))
        with self.assertRaises(ValueError):
            rules.parse_hora("25")

    def test_formato(self):
        self.assertEqual(rules.formato_hora(8.5), "08:30")
        self.assertEqual(rules.formato_hora(17.99), "17:59")
        self.assertEqual(rules.formato_hora(None), "")


if __name__ == "__main__":
    unittest.main()
