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


class PrioridadTest(unittest.TestCase):
    def test_matriz(self):
        casos = [
            ("A", "detenido", "no", "P1"), ("A", "detenido", "no_se", "P1"), ("A", "detenido", None, "P1"),
            ("A", "detenido", "si", "P2"), ("A", "degradado", None, "P2"), ("A", "anomalia", None, "P3"),
            ("B", "detenido", "no", "P2"), ("B", "degradado", None, "P2"), ("B", "anomalia", None, "P3"),
            ("C", "detenido", "no", "P3"), (None, "detenido", "no", "P3"),
        ]
        for crit, cond, resp, esperado in casos:
            with self.subTest(crit=crit, cond=cond, resp=resp):
                self.assertEqual(rules.prioridad_sugerida(crit, cond, resp), esperado)

    def test_mas_alta(self):
        self.assertEqual(rules.prioridad_mas_alta(["P3", "P1", "P2"]), "P1")
        self.assertEqual(rules.prioridad_mas_alta([]), "P3")


class EstadosTest(unittest.TestCase):
    def test_transiciones(self):
        self.assertTrue(rules.transicion_valida("pendiente", "espera_repuesto"))
        self.assertTrue(rules.transicion_valida("espera_repuesto", "en_ejecucion"))
        self.assertTrue(rules.transicion_valida("reparada", "en_ejecucion"))
        self.assertTrue(rules.transicion_valida("en_ejecucion", "anulada"))
        self.assertFalse(rules.transicion_valida("pendiente", "pendiente"))
        self.assertFalse(rules.transicion_valida("en_ejecucion", "cerrada"))   # cerrar es un paso aparte
        self.assertFalse(rules.transicion_valida("cerrada", "pendiente"))      # no se reabre
        self.assertFalse(rules.transicion_valida("anulada", "en_ejecucion"))


class TramosTest(unittest.TestCase):
    from datetime import datetime as _dt
    T0 = _dt(2026, 9, 29, 6, 0)

    def h(self, horas):
        from datetime import timedelta
        return self.T0 + timedelta(hours=horas)

    def test_desglose(self):
        eventos = [("pendiente", self.h(1)), ("espera_repuesto", self.h(3)),
                   ("en_ejecucion", self.h(27)), ("reparada", self.h(29))]
        t = rules.tramos(self.T0, eventos, self.h(30))
        self.assertEqual((t["reaccion"], t["gestion"], t["repuesto"], t["ejecucion"], t["verificacion"]),
                         (1, 2, 24, 2, 1))
        self.assertEqual(t["total"], 30)

    def test_ida_y_vuelta_suma(self):
        eventos = [("en_ejecucion", self.h(0.5)), ("espera_repuesto", self.h(1.5)),
                   ("en_ejecucion", self.h(4.5)), ("reparada", self.h(5.5))]
        t = rules.tramos(self.T0, eventos, self.h(5.5))
        self.assertEqual(t["ejecucion"], 2.0)
        self.assertEqual(t["repuesto"], 3.0)

    def test_hora_desordenada_no_resta(self):
        t = rules.tramos(self.T0, [("pendiente", self.h(2)), ("en_ejecucion", self.h(1))], self.h(3))
        self.assertEqual(t["gestion"], 0.0)
        self.assertEqual(t["ejecucion"], 2.0)

    def test_fuera_de_plazo(self):
        self.assertTrue(rules.fuera_de_plazo(self.T0, 4, self.h(5)))
        self.assertFalse(rules.fuera_de_plazo(self.T0, 4, self.h(3)))
        self.assertFalse(rules.fuera_de_plazo(self.T0, None, self.h(99)))


class FormatoTest(unittest.TestCase):
    def test_duracion(self):
        self.assertEqual(rules.formato_duracion(0.2), "12 min")
        self.assertEqual(rules.formato_duracion(2.5), "2 h 30 min")
        self.assertEqual(rules.formato_duracion(3), "3 h")
        self.assertEqual(rules.formato_duracion(30), "1 d 6 h")
        self.assertEqual(rules.formato_duracion(48), "2 d")
        self.assertEqual(rules.formato_duracion(None), "—")

    def test_folios(self):
        self.assertEqual(rules.folio_ot(7), "OT-0007")
        self.assertEqual(rules.folio_aviso(42), "A-0042")


class DestinosTest(unittest.TestCase):
    MAPA = {"10": "1", "11": "1", "20": "2"}   # estanque → unidad

    def test_sitio_completo_absorbe_todo(self):
        self.assertEqual(rules.normalizar_destinos(["sitio:crianza", "unit:1", "pond:20"], self.MAPA),
                         [("sitio", "crianza")])

    def test_unidad_absorbe_sus_estanques(self):
        self.assertEqual(rules.normalizar_destinos(["unit:1", "pond:10", "pond:20"], self.MAPA),
                         [("unit", "1"), ("pond", "20")])

    def test_descarta_mal_formados_y_duplicados(self):
        self.assertEqual(rules.normalizar_destinos(["pond:10", "pond:10", "basura", "x:1", "pond:"], self.MAPA),
                         [("pond", "10")])

    def test_vacio(self):
        self.assertEqual(rules.normalizar_destinos([], self.MAPA), [])


class NombreRedundanciaTest(unittest.TestCase):
    def test_sube_el_numero_final(self):
        self.assertEqual(rules.nombre_redundancia("Soplador 3", ["Soplador 3"]), "Soplador 4")

    def test_salta_los_ocupados_sin_mayusculas(self):
        self.assertEqual(rules.nombre_redundancia("Soplador 3", ["Soplador 3", "soplador 4"]), "Soplador 5")

    def test_sin_numero_agrega_2(self):
        self.assertEqual(rules.nombre_redundancia("Generador", ["Generador"]), "Generador 2")

    def test_respeta_ceros_a_la_izquierda(self):
        self.assertEqual(rules.nombre_redundancia("Bomba 01", ["Bomba 01"]), "Bomba 02")

    def test_numero_pegado(self):
        self.assertEqual(rules.nombre_redundancia("Soplador N3", ["Soplador N3"]), "Soplador N4")


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
