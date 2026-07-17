"""Verificación del motor de calidad de agua (sin BD).

Ejecutar desde la raíz con PYTHONPATH=raíz:
    ./.venv/Scripts/python.exe scripts/verify_water_quality.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.water_quality import (  # noqa: E402
    do_saturation_mg_l,
    expected_saturation_pct,
    unionized_ammonia_n,
    total_nitrogen,
    evaluate_oxygen,
    evaluate_biofilter,
)


def approx(a, b, tol):
    return abs(a - b) <= tol


checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


print("== Solubilidad O2 (agua dulce) ==")
# Referencias conocidas a nivel del mar (Benson-Krause), salinidad 0
cs20_sea = do_saturation_mg_l(20.0, altitude_m=0.0)
cs10_sea = do_saturation_mg_l(10.0, altitude_m=0.0)
print(f"  Cs(20°C, 0 m)  = {cs20_sea:.3f} mg/L (ref ~9.08)")
print(f"  Cs(10°C, 0 m)  = {cs10_sea:.3f} mg/L (ref ~11.29)")
check("Cs(20°C) ~ 9.08", approx(cs20_sea, 9.08, 0.10))
check("Cs(10°C) ~ 11.29", approx(cs10_sea, 11.29, 0.15))

# A 170 m la solubilidad baja ~2%
cs20_site = do_saturation_mg_l(20.0)  # altitud default 170 m
print(f"  Cs(20°C, 170 m) = {cs20_site:.3f} mg/L")
check("altitud 170 m reduce Cs ~1-3%", 0.97 <= cs20_site / cs20_sea <= 0.99)

print("== Saturación teórica y consistencia ==")
# Si O2 medido == Cs(T,sitio) entonces saturación ~100%
sat = expected_saturation_pct(cs20_site, 20.0)
check("O2=Cs -> sat ~100%", approx(sat, 100.0, 0.5))

# Terna coherente -> consistency ok, alarma ok
r = evaluate_oxygen(do_mg_l=cs20_site, water_temp_c=20.0, saturation_pct=round(sat, 1))
check("terna coherente -> consistente", r.consistency_flag == "ok")
check("saturación ~100% -> sin alarma", r.alarm_level == "ok")

# Saturación ingresada absurda (150) frente a teórica ~100 -> sospechoso
r2 = evaluate_oxygen(do_mg_l=cs20_site, water_temp_c=20.0, saturation_pct=150.0)
check("saturación incoherente -> sospechoso", r2.consistency_flag == "sospechoso")

# Saturación baja -> alerta/alarma
r3 = evaluate_oxygen(do_mg_l=None, water_temp_c=None, saturation_pct=65.0)
check("sat 65% -> alerta", r3.alarm_level == "alerta")
r4 = evaluate_oxygen(do_mg_l=None, water_temp_c=None, saturation_pct=55.0)
check("sat 55% -> alarma", r4.alarm_level == "alarma")

print("== Amonio no ionizado (NH3) ==")
# A pH 7 la fracción no ionizada es muy baja; a pH 9 sube fuerte
nh3_ph7 = unionized_ammonia_n(nh4_n=1.0, ph=7.0, temp_c=20.0)
nh3_ph9 = unionized_ammonia_n(nh4_n=1.0, ph=9.0, temp_c=20.0)
print(f"  NH3-N (1 mg/L TAN, pH7, 20°C) = {nh3_ph7:.4f}")
print(f"  NH3-N (1 mg/L TAN, pH9, 20°C) = {nh3_ph9:.4f}")
check("NH3 crece con pH", nh3_ph9 > nh3_ph7 * 10)
check("NH3 a pH7 es bajo (<0.01)", nh3_ph7 < 0.01)

# Amonio total moderado a pH alto dispara alarma; a pH neutro no
alarma_alta = evaluate_biofilter({
    "in_nh4_n": 1.0, "in_no2_n": 0.05, "in_no3_n": 5.0, "in_ph": 8.8, "in_temp_c": 20.0,
    "out_nh4_n": 0.2, "out_no2_n": 0.1, "out_no3_n": 5.7, "out_ph": 8.7, "out_temp_c": 20.0,
})
check("amonio a pH alto -> alarma NH3", alarma_alta.alarm_level == "alarma")

neutro = evaluate_biofilter({
    "in_nh4_n": 1.0, "in_no2_n": 0.05, "in_no3_n": 5.0, "in_ph": 7.0, "in_temp_c": 18.0,
    "out_nh4_n": 0.2, "out_no2_n": 0.08, "out_no3_n": 5.77, "out_ph": 7.0, "out_temp_c": 18.0,
})
check("mismo amonio a pH neutro -> sin alarma", neutro.alarm_level == "ok")

print("== Balance de nitrógeno ==")
# TN_in = 1+0.05+5 = 6.05 ; TN_out = 0.2+0.08+5.77 = 6.05 -> balance ok
check("TN calculado correcto", approx(total_nitrogen(1.0, 0.05, 5.0), 6.05, 1e-9))
check("balance conservado -> ok", neutro.n_balance_flag == "ok")

# Fuga de N: salida mucho menor -> sospechoso
fuga = evaluate_biofilter({
    "in_nh4_n": 1.0, "in_no2_n": 0.05, "in_no3_n": 5.0, "in_ph": 7.0, "in_temp_c": 18.0,
    "out_nh4_n": 0.2, "out_no2_n": 0.08, "out_no3_n": 3.0, "out_ph": 7.0, "out_temp_c": 18.0,
})
check("desbalance de N -> sospechoso", fuga.n_balance_flag == "sospechoso")

print("== ΔpH / Δtemp (validación) ==")
delta = evaluate_biofilter({
    "in_ph": 7.0, "out_ph": 8.0, "in_temp_c": 18.0, "out_temp_c": 20.0,
    "in_nh4_n": 0.5, "in_no2_n": 0.05, "in_no3_n": 5.0,
    "out_nh4_n": 0.3, "out_no2_n": 0.05, "out_no3_n": 5.2,
})
check("ΔpH 1.0 -> sospechoso", delta.ph_delta_flag == "sospechoso")
check("Δtemp 2°C -> sospechoso", delta.temp_delta_flag == "sospechoso")

print("== Nitrito ==")
nitrito = evaluate_biofilter({
    "in_nh4_n": 0.1, "in_no2_n": 0.6, "in_no3_n": 5.0, "in_ph": 7.0, "in_temp_c": 18.0,
    "out_nh4_n": 0.1, "out_no2_n": 0.6, "out_no3_n": 5.0, "out_ph": 7.0, "out_temp_c": 18.0,
})
check("nitrito 0.6 mg/L -> alarma", nitrito.alarm_level == "alarma")

print()
failed = [n for n, ok in checks if not ok]
print(f"RESULTADO: {len(checks) - len(failed)}/{len(checks)} OK")
if failed:
    print("FALLARON:", ", ".join(failed))
    sys.exit(1)
print("Todos los checks pasaron.")
