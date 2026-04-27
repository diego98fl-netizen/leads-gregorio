"""
stages/score.py — Motor de scoring para Gregario Leads Pipeline.

Lee reglas desde inputs/icp.yaml y blacklist desde inputs/blacklist.csv,
y asigna a cada lead (empresa + contactos) un score numérico y un tier
final: descarte / bronce / plata / oro.

API principal:
    from stages.score import load_icp, load_blacklist, score_lead

    icp = load_icp("inputs/icp.yaml")
    bl  = load_blacklist("inputs/blacklist.csv")
    resultado = score_lead(empresa, contactos, icp, bl)

Ejecutar como script para ver un ejemplo sintético:
    python -m stages.score
"""

from __future__ import annotations

import csv
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import yaml

logger = logging.getLogger("gregario.score")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# Carga de configuración
# ---------------------------------------------------------------------------

def load_icp(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        icp = yaml.safe_load(f)
    version = icp.get("meta", {}).get("version", "?")
    logger.info("ICP cargado desde %s (versión %s)", path, version)
    return icp


def load_blacklist(path: str | Path) -> list[dict[str, str]]:
    path = Path(path)
    if not path.exists():
        logger.warning("Blacklist no encontrada en %s — continuando sin filtrado", path)
        return []
    with path.open(encoding="utf-8") as f:
        filas = list(csv.DictReader(f))
    logger.info("Blacklist cargada: %d empresas", len(filas))
    return filas


# ---------------------------------------------------------------------------
# Normalización y fuzzy matching
# ---------------------------------------------------------------------------

DEFAULT_SUFIJOS_LEGALES = (
    "sa de cv", "s.a. de c.v.", "sapi de cv", "sapi",
    "s de rl", "s. de r.l.", "srl",
    "sc", "s.c.", "sa", "s.a.",
)


def normalize_name(nombre: str, sufijos: Iterable[str] = DEFAULT_SUFIJOS_LEGALES) -> str:
    """lowercase + sin acentos + sin sufijos legales (al final del nombre)."""
    if not nombre:
        return ""
    txt = unicodedata.normalize("NFKD", nombre)
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    txt = txt.lower()
    txt = re.sub(r"[.,;:&/]", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    for suf in sorted(sufijos, key=len, reverse=True):
        suf_norm = re.sub(r"[.,;:]", " ", suf.lower())
        suf_norm = re.sub(r"\s+", " ", suf_norm).strip()
        if not suf_norm:
            continue
        if txt == suf_norm or txt.endswith(" " + suf_norm):
            txt = txt[: len(txt) - len(suf_norm)].rstrip()
    return txt


def is_blacklisted(
    nombre: str,
    blacklist: list[dict[str, str]],
    threshold: int = 85,
) -> tuple[bool, str | None]:
    if not blacklist or not nombre:
        return False, None
    candidato = normalize_name(nombre)
    if not candidato:
        return False, None
    for fila in blacklist:
        bl_norm = (fila.get("nombre_normalizado") or "").strip()
        if not bl_norm:
            bl_norm = normalize_name(fila.get("nombre_original", ""))
        ratio = SequenceMatcher(None, candidato, bl_norm).ratio() * 100
        if ratio >= threshold:
            estado = fila.get("estado", "?")
            original = fila.get("nombre_original", bl_norm)
            return True, f"match con '{original}' (ratio={ratio:.1f}, estado={estado})"
    return False, None


# ---------------------------------------------------------------------------
# Scoring de empresa
# ---------------------------------------------------------------------------

def score_empresa(empresa: dict[str, Any], icp: dict[str, Any]) -> dict[str, Any]:
    """Suma señales positivas y negativas presentes en `empresa['senales']`."""
    senales = empresa.get("senales", {}) or {}
    desglose: list[dict[str, Any]] = []
    total = 0
    descartar = False
    motivo_descarte: str | None = None

    for s in icp["senales_empresa"]["positivas"]:
        if senales.get(s["codigo"]):
            total += s["peso"]
            desglose.append({"codigo": s["codigo"], "peso": s["peso"], "tipo": "+"})

    for s in icp["senales_empresa"]["negativas"]:
        if senales.get(s["codigo"]):
            total += s["peso"]
            desglose.append({"codigo": s["codigo"], "peso": s["peso"], "tipo": "-"})
            if s.get("descarte_automatico"):
                descartar = True
                motivo_descarte = f"señal con descarte automático: {s['codigo']}"

    return {
        "score": total,
        "desglose": desglose,
        "descartar": descartar,
        "motivo_descarte": motivo_descarte,
    }


# ---------------------------------------------------------------------------
# Scoring de contacto
# ---------------------------------------------------------------------------

EMAIL_GENERICO_RE = re.compile(
    r"^(contacto|info|ventas|hola|contact|sales|hello|admin|atencion|soporte)@",
    re.IGNORECASE,
)


def _match_cargo(cargo: str, lista_cargos: Iterable[str]) -> bool:
    if not cargo:
        return False
    cargo_n = normalize_name(cargo)
    return any(normalize_name(c) in cargo_n for c in lista_cargos)


def _tier_de_cargo(cargo: str, icp: dict[str, Any]) -> tuple[str | None, int]:
    tiers = icp["contactos_tiers"]
    if _match_cargo(cargo, tiers["excluidos"]["cargos"]):
        return "excluido", 0
    for tier_id in ("tier_1", "tier_2", "tier_3"):
        if _match_cargo(cargo, tiers[tier_id]["cargos"]):
            return tier_id, tiers[tier_id]["peso"]
    return None, 0


def _calidad_contacto(contacto: dict[str, Any]) -> tuple[str, int]:
    email = (contacto.get("email") or "").strip().lower()
    es_generico = bool(email) and bool(EMAIL_GENERICO_RE.match(email))
    email_directo = bool(email) and not es_generico
    telefono = bool(contacto.get("telefono"))
    linkedin = bool(contacto.get("linkedin"))
    basicos = all(contacto.get(k) for k in ("nombre", "cargo", "empresa"))

    # Email genérico es descarte automático según ICP.
    if es_generico:
        return "descarte_email_generico", -100
    if not basicos:
        return "insuficiente", 0
    # 🥇 Oro: nombre+cargo+empresa+telefono+email_directo+linkedin
    if telefono and email_directo and linkedin:
        return "oro", 30
    # 🥈 Plata: nombre+cargo+empresa + (telefono O email_directo)
    if telefono or email_directo:
        return "plata", 20
    # 🥉 Bronce: nombre+cargo+empresa + linkedin
    if linkedin:
        return "bronce", 10
    return "insuficiente", 0


def score_contacto(contacto: dict[str, Any], icp: dict[str, Any]) -> dict[str, Any]:
    tier_id, peso_tier = _tier_de_cargo(contacto.get("cargo", ""), icp)
    nivel, peso_calidad = _calidad_contacto(contacto)

    descartar = tier_id == "excluido" or nivel == "descarte_email_generico"
    motivo: str | None = None
    if tier_id == "excluido":
        motivo = f"cargo excluido: {contacto.get('cargo')!r}"
    elif nivel == "descarte_email_generico":
        motivo = f"único canal es email genérico: {contacto.get('email')!r}"
    elif nivel == "insuficiente":
        # No descartamos al contacto, pero score=0 y se logea.
        logger.debug("Contacto con datos insuficientes: %s", contacto.get("nombre"))

    return {
        "score": peso_tier + peso_calidad,
        "tier": tier_id,
        "peso_tier": peso_tier,
        "calidad": nivel,
        "peso_calidad": peso_calidad,
        "descartar": descartar,
        "motivo_descarte": motivo,
    }


# ---------------------------------------------------------------------------
# Orquestación: score_lead
# ---------------------------------------------------------------------------

def _tier_final(score: int, rangos: dict[str, dict[str, int]]) -> str:
    for nombre in ("oro", "plata", "bronce", "descarte"):
        r = rangos[nombre]
        if r["min"] <= score <= r["max"]:
            return nombre
    return "descarte"


def _descarte(empresa: str, motivo: str, **extra: Any) -> dict[str, Any]:
    logger.warning("DESCARTE '%s': %s", empresa, motivo)
    return {
        "empresa": empresa,
        "score_total": -1000,
        "tier_final": "descarte",
        "descartar": True,
        "motivo_descarte": motivo,
        **extra,
    }


def score_lead(
    empresa: dict[str, Any],
    contactos: list[dict[str, Any]],
    icp: dict[str, Any],
    blacklist: list[dict[str, str]],
) -> dict[str, Any]:
    nombre_empresa = empresa.get("nombre", "")

    # 1) Blacklist (descarte automático).
    threshold = icp["blacklist"]["fuzzy_match"]["score_minimo"]
    en_bl, motivo_bl = is_blacklisted(nombre_empresa, blacklist, threshold=threshold)
    if en_bl:
        return _descarte(nombre_empresa, f"blacklist: {motivo_bl}")

    # 2) Empresa.
    res_empresa = score_empresa(empresa, icp)
    if res_empresa["descartar"]:
        return _descarte(
            nombre_empresa,
            res_empresa["motivo_descarte"] or "empresa con descarte automático",
            score_empresa=res_empresa["score"],
            desglose_empresa=res_empresa["desglose"],
        )

    # 3) Contactos: scorear todos, descartar inválidos, elegir el mejor.
    scoreados = [{"contacto": c, "_s": score_contacto(c, icp)} for c in contactos]
    validos = [s for s in scoreados if not s["_s"]["descartar"] and s["_s"]["tier"] is not None]

    if not validos:
        return _descarte(
            nombre_empresa,
            "sin contactos válidos (todos excluidos, sin tier reconocible o con email genérico)",
            score_empresa=res_empresa["score"],
            desglose_empresa=res_empresa["desglose"],
        )

    mejor = max(validos, key=lambda s: s["_s"]["score"])
    s_emp = res_empresa["score"]
    s_con = mejor["_s"]["score"]
    score_total = s_emp + s_con
    tier_final = _tier_final(score_total, icp["scoring"]["rangos"])

    logger.info(
        "Scored '%s': empresa=%d + contacto=%d (tier=%s, calidad=%s) = %d → %s",
        nombre_empresa, s_emp, s_con,
        mejor["_s"]["tier"], mejor["_s"]["calidad"],
        score_total, tier_final.upper(),
    )

    return {
        "empresa": nombre_empresa,
        "score_empresa": s_emp,
        "score_contacto": s_con,
        "score_total": score_total,
        "tier_final": tier_final,
        "descartar": False,
        "mejor_contacto": {
            "nombre": mejor["contacto"].get("nombre"),
            "cargo": mejor["contacto"].get("cargo"),
            "tier": mejor["_s"]["tier"],
            "calidad": mejor["_s"]["calidad"],
        },
        "desglose_empresa": res_empresa["desglose"],
    }


# ---------------------------------------------------------------------------
# Demo sintético
# ---------------------------------------------------------------------------

def _demo() -> None:
    base = Path(__file__).resolve().parent.parent
    icp = load_icp(base / "inputs" / "icp.yaml")
    blacklist = load_blacklist(base / "inputs" / "blacklist.csv")

    print("\n========== EJEMPLO 1 — Lead 🥇 oro esperado ==========\n")
    empresa1 = {
        "nombre": "Lácteos del Bajío SA de CV",
        "senales": {
            "presencia_horeca_y_tradicional": True,        # +15
            "equipo_terreno_activo": True,                  # +20
            "venta_directa_o_distribuidores_propios": True, # +10
            "web_activa_clientes": True,                    # +5
            "gerente_foodservice_horeca": True,             # +25
        },
    }
    contactos1 = [
        {  # Tier 1 + 🥇 oro
            "nombre": "María López",
            "cargo": "Gerente Foodservice",
            "empresa": empresa1["nombre"],
            "telefono": "+52 55 1234 5678",
            "email": "maria.lopez@lacteosdelbajio.com",
            "linkedin": "https://linkedin.com/in/marialopez",
        },
        {  # Excluido (Compras)
            "nombre": "Juan Pérez",
            "cargo": "Gerente de Compras",
            "empresa": empresa1["nombre"],
            "email": "juan.perez@lacteosdelbajio.com",
        },
    ]
    for k, v in score_lead(empresa1, contactos1, icp, blacklist).items():
        print(f"  {k}: {v}")

    print("\n========== EJEMPLO 2 — Blacklist (debe descartar) ==========\n")
    empresa2 = {"nombre": "Sigma Alimentos S.A. de C.V.", "senales": {}}
    for k, v in score_lead(empresa2, contactos1[:1], icp, blacklist).items():
        print(f"  {k}: {v}")

    print("\n========== EJEMPLO 3 — Email genérico único canal ==========\n")
    empresa3 = {
        "nombre": "Conservas del Centro",
        "senales": {"equipo_terreno_activo": True, "web_activa_clientes": True},
    }
    contactos3 = [{
        "nombre": "Sin Nombre",
        "cargo": "Gerente Comercial",
        "empresa": empresa3["nombre"],
        "email": "ventas@conservasdelcentro.mx",
    }]
    for k, v in score_lead(empresa3, contactos3, icp, blacklist).items():
        print(f"  {k}: {v}")

    print("\n========== EJEMPLO 4 — <50 empleados (descarte automático) ==========\n")
    empresa4 = {
        "nombre": "Pequeña Panadería Artesanal",
        "senales": {"menos_50_empleados": True, "web_activa_clientes": True},
    }
    for k, v in score_lead(empresa4, contactos1[:1], icp, blacklist).items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    _demo()
