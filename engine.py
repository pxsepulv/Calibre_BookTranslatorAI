from __future__ import annotations
import base64
import hashlib
from io import BytesIO
import json
import os
import re
import shutil
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup, NavigableString

try:
    from PIL import Image
    TIENE_PIL = True
except ImportError:
    TIENE_PIL = False

try:
    from calibre.constants import config_dir
except ImportError:
    config_dir = os.path.expanduser("~/.calibre_book_translator_ai")

URL_API = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"

AJUSTES_SEGURIDAD = [
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
]

class ErrorNoRecuperable(Exception):
    """Errores fatales de configuración, autenticación, SSL o cuota diaria."""
    pass


class ErrorCapitulo(Exception):
    """Errores que afectan únicamente al capítulo actual y permiten seguir con el lote."""
    pass


class BloqueRechazado(ErrorCapitulo):
    """El modelo rechazó este contenido (filtro o recitación)."""
    pass


class RespuestaTruncada(ErrorCapitulo):
    """Indica que el modelo alcanzó MAX_TOKENS y el bloque debe subdividirse."""
    pass
# ---- Parámetros de costo ----
PALABRAS_POR_BLOQUE = 2500
TOPE_TOKENS = 0   # 0 = sin tope. Ej.: 3_000_000 detiene el lote al superarlo

USO = {"llamadas": 0, "entrada": 0, "salida": 0, "pensamiento": 0, "cache": 0}

def reiniciar_uso():
    for k in USO:
        USO[k] = 0

def _registrar_uso(data: dict):
    u = data.get("usageMetadata", {}) or {}
    USO["llamadas"] += 1
    USO["entrada"] += u.get("promptTokenCount", 0)
    USO["salida"] += u.get("candidatesTokenCount", 0)
    USO["pensamiento"] += u.get("thoughtsTokenCount", 0)
    USO["cache"] += u.get("cachedContentTokenCount", 0)

def resumen_uso() -> str:
    return (f"Uso API: {USO['llamadas']} llamadas | entrada {USO['entrada']:,} "
            f"(caché {USO['cache']:,}) | salida {USO['salida']:,} | pensamiento {USO['pensamiento']:,} tokens")

def _config_generacion(modelo: str, temperatura: float = 0.2) -> dict:
    cfg = {"temperature": temperatura}
    m = modelo.lower()
    if "2.5" in m and "pro" not in m:
        cfg["thinkingConfig"] = {"thinkingBudget": 0}      # Flash / Flash-Lite
    elif "2.5" in m:
        cfg["thinkingConfig"] = {"thinkingBudget": 128}    # Pro no se puede apagar
    elif "gemini-3" in m:
        cfg["thinkingConfig"] = {"thinkingLevel": "low"}
    return cfg


# ==============================================================================
# PLANTILLAS DE GUÍA SEGÚN EL TIPO DE OBRA
# ==============================================================================
PROMPT_NOVELA_LIGERA = """
Eres un director editorial y coordinador de traducción literaria.
A continuación te entrego el texto completo de un libro en inglés.

{BLOQUE_SERIE}

Analiza exhaustivamente la totalidad del texto y genera una "Guía de Traducción" en español neutro con el siguiente esquema estricto:

1. DIRECTIVAS DE ESTRUCTURA XHTML:
- Mantén intactas todas las etiquetas HTML/XML (<p>, <div>, <em>, <strong>, <img>, etc.), clases (class="..."), identificadores (id="...") y atributos (href, src).
- Traduce únicamente el contenido textual visible entre las etiquetas.
- PROHIBIDO usar asteriscos de Markdown (*cursiva* o **negrita**) dentro del texto; para dar énfasis o cursiva usa exclusivamente las etiquetas XHTML <em>...</em> y <strong>...</strong>.
- Prohibido modificar nombres de archivos, rutas de imágenes o enlaces.

2. CONVENCIONES EDITORIALES (RAE):
- Transforma las comillas inglesas ("...") de diálogo en rayas largas de diálogo (—).
- Respeta la puntuación estándar: raya pegada a la primera palabra (—Hola), minúscula tras la raya en acotaciones con verbo de habla (—dijo él—.), y punto tras la raya en acotaciones de acción (—Se puso de pie.).
- Saltos de escena vacíos: unificar en <p class="scene-break" style="text-align: center;">***</p>

3. FICHA DE PERSONAJES:
- Lista detallada con: Nombre original | Género gramatical | Registro de voz y tratamiento (tú / usted) | Rol breve.
   (Fija el género gramatical sin ambigüedades a partir de todo el contexto del libro).

4. GLOSARIO DE TÉRMINOS Y LUGARES (30 a 50 entradas):
- "Término en inglés" = "Traducción oficial/recomendada al español" (añade una breve nota si se requiere evitar calcos o si es un término consagrado en la comunidad/adaptación oficial de manga o anime).

5. TONO GENERAL Y REGISTRO:
- Nivel de formalidad, estilo narrativo y tratamiento de diálogos.

Entrega ÚNICAMENTE las directivas de la guía, listas para ser usadas como System Instruction.
"""

PROMPT_TECNICO_MEDICO = """
Eres un editor científico, médico y especialista en informática en salud.
A continuación te entrego el texto completo de un libro o manual técnico en inglés.

{BLOQUE_SERIE}

Analiza exhaustivamente la totalidad del texto y genera una "Guía de Traducción Técnica" en español formal con el siguiente esquema estricto:

1. DIRECTIVAS DE ESTRUCTURA XHTML:
- Mantén absolutamente intactas todas las etiquetas (<p>, <code>, <pre>, <table>, <div>, <img>, clases, identificadores y estilos CSS).
- Traduce únicamente el contenido explicativo legible. Prohibido alterar bloques de código fuente, scripts o sintaxis de datos (JSON, XML).
- PROHIBIDO usar asteriscos de Markdown (*cursiva* o **negrita**); utiliza siempre etiquetas XHTML (<em>, <strong>).
- Prohibido modificar rutas de esquemas, imágenes o hipervínculos.

2. GLOSARIO DE TÉRMINOS TÉCNICOS Y CLÍNICOS (40 a 60 entradas):
- "Término en inglés" = "Traducción técnica estandarizada al español" (evitar calcos literales, falsos amigos y priorizar terminología biomédica normalizada).

3. POLÍTICA DE ACRÓNIMOS Y ESTÁNDARES INTERNACIONALES:
- Lista explícita de siglas universales que NO se traducen (ej. FHIR, HL7, DICOM, SNOMED CT, REST API, OAuth).
- Lista explícita de siglas con traducción obligatoria al español formal (ej. EHR -> HCE [Historia Clínica Electrónica], CDSS -> SADC [Sistema de Apoyo a la Decisión Clínica], PHI -> IPS [Información de Salud Protegida]).

4. REGISTRO ACADÉMICO Y ESTILO:
- Voz formal, impersonal y objetiva (tercera persona o pasiva refleja).
- Rigor biomédico y farmacológico estricto.
- Puntuación técnica decimal y unidades del Sistema Internacional.
   (NO incluir reglas de diálogo literario ni rayas de conversación).

Entrega ÚNICAMENTE las directivas de la guía, listas para ser usadas como System Instruction.
"""

PROMPT_NOVELA_GENERAL = """
Eres un corrector de estilo y traductor literario editorial.
A continuación te entrego el texto completo de una novela en inglés.

{BLOQUE_SERIE}

Analiza exhaustivamente la totalidad del texto y genera una "Guía de Traducción" en español neutro con el siguiente esquema estricto:

1. DIRECTIVAS DE ESTRUCTURA XHTML:
- Mantén intactas todas las etiquetas, clases, imágenes y jerarquía visual del documento.
- Traduce únicamente el texto visible entre etiquetas.
- PROHIBIDO usar sintaxis Markdown (*cursiva* o **negrita**); usa exclusivamente etiquetas XHTML <em> y <strong>.

2. DIÁLOGOS SEGÚN LA RAE:
- Rayas largas de diálogo (—) pegadas al texto.
- Puntuación normativa: minúscula tras la raya para verbos de habla (—dijo ella—.), punto para acciones independientes (—Cerró la puerta.).

3. CONCORDANCIA Y PERSONAJES:
- Nombre original | Género gramatical fijado por contexto | Tratamiento (tú vs. usted) y tono propio de cada personaje.

4. GLOSARIO NARRATIVO (30 a 50 entradas):
- Lugares, expresiones idiomáticas y términos recurrentes traducidos con naturalidad en español neutro.

5. TONO GENERAL Y REGISTRO:
- Voz del narrador, tiempo verbal dominante y estilo general.

Entrega ÚNICAMENTE las directivas de la guía, listas para ser usadas como System Instruction.
"""

PROMPT_ENSAYO = """
Eres un editor académico y traductor de no ficción y ensayos.
A continuación te entrego el texto completo de una obra en inglés.

{BLOQUE_SERIE}

Analiza exhaustivamente la totalidad del texto y genera una "Guía de Traducción" en español formal con:

1. DIRECTIVAS XHTML: Conserva intactas notas al pie (tags <a>, <sup>), citas en bloque (<blockquote>), imágenes y maquetación. Prohibido usar asteriscos Markdown (*...*), usa etiquetas <em> y <strong>.
2. GLOSARIO CONCEPTUAL (30 a 50 términos): Definición unificada de conceptos clave, escuelas teóricas, instituciones y términos históricos o filosóficos.
3. POLÍTICA DE CITAS Y REFERENCIAS: Criterio para títulos bibliográficos y citas textuales de autores.
4. TONO EXPOSITIVO: Nivel de formalidad académica, claridad pedagógica y rigor expositivo.

Entrega ÚNICAMENTE las directivas de la guía, listas para ser usadas como System Instruction.
"""

DICCIONARIO_PROMPTS = {
    "Novela Ligera / Manga / Fantasía": PROMPT_NOVELA_LIGERA,
    "Técnico / Medicina e Informática Clínica": PROMPT_TECNICO_MEDICO,
    "Novela General / Ficción Narrativa": PROMPT_NOVELA_GENERAL,
    "Ensayo / No Ficción / Divulgación": PROMPT_ENSAYO,
}


# ==============================================================================
# GESTIÓN DE DIRECTORIO DE TRABAJO FUERA DE LA BIBLIOTECA DE CALIBRE
# ==============================================================================
def _obtener_directorio_trabajo_libro(ruta_epub: str) -> str:
    nombre_base, _ = os.path.splitext(os.path.basename(ruta_epub))
    hash_corto = hashlib.sha1(os.path.abspath(ruta_epub).encode("utf-8")).hexdigest()[:10]
    nombre_limpio = re.sub(r'[^a-zA-Z0-9_-]', '_', nombre_base)[:40]

    base_work = os.path.join(config_dir, "plugins", "book_translator_ai_work", f"{nombre_limpio}_{hash_corto}")
    os.makedirs(base_work, exist_ok=True)

    carpeta_epub = os.path.dirname(ruta_epub)
    antiguo_prog = os.path.join(carpeta_epub, f"progreso_{nombre_base}.json")
    nuevo_prog = os.path.join(base_work, "progreso.json")
    if os.path.exists(antiguo_prog) and not os.path.exists(nuevo_prog):
        try:
            shutil.move(antiguo_prog, nuevo_prog)
        except Exception:
            pass

    antigua_guia = os.path.join(carpeta_epub, f"guia_{nombre_base}.txt")
    nueva_guia = os.path.join(base_work, "guia.txt")
    if os.path.exists(antigua_guia) and not os.path.exists(nueva_guia):
        try:
            shutil.move(antigua_guia, nueva_guia)
        except Exception:
            pass

    antigua_imgs = os.path.join(carpeta_epub, f"img_traducidas_{nombre_base}")
    nueva_imgs = os.path.join(base_work, "img_traducidas")
    if os.path.isdir(antigua_imgs) and not os.path.exists(nueva_imgs):
        try:
            shutil.move(antigua_imgs, nueva_imgs)
        except Exception:
            pass

    return base_work


def obtener_ruta_progreso(ruta_epub: str) -> str:
    return os.path.join(_obtener_directorio_trabajo_libro(ruta_epub), "progreso.json")


def obtener_ruta_guia_auto(ruta_epub: str) -> str:
    return os.path.join(_obtener_directorio_trabajo_libro(ruta_epub), "guia.txt")


def obtener_carpeta_imagenes_traducidas(ruta_epub: str, crear: bool = False) -> str:
    ruta_dir = os.path.join(_obtener_directorio_trabajo_libro(ruta_epub), "img_traducidas")
    if crear:
        os.makedirs(ruta_dir, exist_ok=True)
    return ruta_dir


def cargar_progreso_disco(ruta_epub: str) -> dict:
    ruta = obtener_ruta_progreso(ruta_epub)
    if os.path.exists(ruta):
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def guardar_progreso_disco(ruta_epub: str, progreso: dict):
    ruta = obtener_ruta_progreso(ruta_epub)
    ruta_tmp = ruta + ".tmp"
    with open(ruta_tmp, "w", encoding="utf-8") as f:
        json.dump(progreso, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(ruta_tmp, ruta)


# ==============================================================================
# UTILIDADES DE TEXTO, PAUSA INTERRUMPIBLE Y LIMPIEZA
# ==============================================================================
def pausa(seg: float, detener_cb=None):
    pasos = max(1, int(seg * 2))
    for _ in range(pasos):
        if detener_cb and detener_cb():
            raise InterruptedError("Traducción pausada por el usuario.")
        time.sleep(0.5)

def _decodificar_xhtml_seguro(bytes_xhtml: bytes) -> str:
    # Intentar detectar la codificación desde la declaración XML <?xml ... encoding="..."?>
    inicio = bytes_xhtml[:100]
    match = re.search(b'encoding=["\']([a-zA-Z0-9._-]+)["\']', inicio)
    if match:
        enc = match.group(1).decode("utf-8", errors="ignore")
        try:
            return bytes_xhtml.decode(enc)
        except Exception:
            pass
    try:
        return bytes_xhtml.decode("utf-8")
    except UnicodeDecodeError:
        return bytes_xhtml.decode("latin-1", errors="ignore")

def contar_palabras(html: str) -> int:
    return len(BeautifulSoup(html, "html.parser").get_text(" ", strip=True).split())


def reparar_markdown_residual(texto_xhtml: str) -> str:
    if not texto_xhtml:
        return ""
    _NEGRITA = re.compile(r'(?<!\*)\*\*(?!\s|\*)([^\*\n<>]+?)(?<!\s|\*)\*\*(?!\*)')
    _CURSIVA = re.compile(r'(?<!\*)\*(?!\s|\*)([^\*\n<>]+?)(?<!\s|\*)\*(?!\*)')

    salida, en_codigo = [], 0
    for tok in re.split(r'(<[^>]+>)', texto_xhtml):
        if tok.startswith('<'):
            m = re.match(r'<(/?)(code|pre|kbd|samp|script|style)\b', tok, re.I)
            if m:
                en_codigo = max(0, en_codigo + (-1 if m.group(1) else 1))
            salida.append(tok)
        elif en_codigo:
            salida.append(tok)
        else:
            salida.append(_CURSIVA.sub(r'<em>\1</em>', _NEGRITA.sub(r'<strong>\1</strong>', tok)))
    return "".join(salida)


def quitar_cercas_codigo(texto: str) -> str:
    if not texto:
        return ""
    patron = r"^```(?:html|xhtml|xml|markdown|text)?\s*\n(.*?)\n```$"
    m = re.search(patron, texto.strip(), re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else texto.strip()


def limpiar_bloques_markdown(texto: str) -> str:
    return reparar_markdown_residual(quitar_cercas_codigo(texto))


def actualizar_idioma_xhtml(xhtml: str) -> str:
    return re.sub(r'(?<!\w)((?:xml:)?lang=["\'])en[^"\']*(["\'])', r'\1es\2', xhtml, flags=re.IGNORECASE)


def actualizar_idioma_opf(opf_texto: str) -> str:
    return re.sub(
        r'(<dc:language[^>]*>)\s*en[^<]*(</dc:language>)',
        r'\1es\2',
        opf_texto,
        flags=re.IGNORECASE
    )

def _titulo_desde_encabezado(cuerpo_orig: str, cuerpo_trad: str, titulo_orig: str) -> str:
    ho = BeautifulSoup(cuerpo_orig, "html.parser").find(re.compile(r"^h[1-3]$"))
    ht = BeautifulSoup(cuerpo_trad, "html.parser").find(re.compile(r"^h[1-3]$"))
    if ho and ht and ho.get_text(" ", strip=True) == titulo_orig:
        return ht.get_text(" ", strip=True)
    return ""

# ==============================================================================
# VALIDACIÓN ESTRUCTURAL DE BLOQUES XHTML Y DESENVOLTURA DE CONTENEDORES
# ==============================================================================
def estructura_xhtml(html: str) -> dict:
    s = BeautifulSoup(html, "html.parser")
    return {
        "p": sum(1 for p in s.find_all("p") if re.search(r"\w", p.get_text())),
        "img": [t.get("src") for t in s.find_all("img")],
        "id": sorted(t["id"] for t in s.find_all(id=True)),
        "href": sorted(t["href"] for t in s.find_all(href=True)),
    }


def validar_bloque(orig: str, trad: str) -> str:
    if not trad or not trad.strip():
        return "El bloque devuelto está vacío"

    a, b = estructura_xhtml(orig), estructura_xhtml(trad)
    for k in ("img", "id", "href"):
        if a[k] != b[k]:
            return f"Cambió '{k}' respecto al original"

    if a["p"] > 0:
        diferencia = abs(b["p"] - a["p"])
        tolerancia = max(2, int(a["p"] * 0.10))
        if diferencia > tolerancia:
            return f"Párrafos: {a['p']} → {b['p']}"

    return ""


_SVG_CASE = {
    "lineargradient": "linearGradient", "radialgradient": "radialGradient",
    "clippath": "clipPath", "foreignobject": "foreignObject", "textpath": "textPath",
    "viewbox": "viewBox", "preserveaspectratio": "preserveAspectRatio",
    "gradientunits": "gradientUnits", "gradienttransform": "gradientTransform",
    "clippathunits": "clipPathUnits", "patternunits": "patternUnits",
    "markerwidth": "markerWidth", "markerheight": "markerHeight",
    "refx": "refX", "refy": "refY", "stddeviation": "stdDeviation",
}

def _restaurar_svg(html):
    def arreglar(m):
        s = m.group(0)
        for bajo, orig in _SVG_CASE.items():
            s = re.sub(rf'(?<=[<\s/]){bajo}(?=[\s=>/])', orig, s)
        return s
    return re.sub(r'<svg\b.*?</svg>', arreglar, html, flags=re.S | re.I)

def _nodo_a_html(nodo):
    res = nodo.output_ready(formatter="minimal") if isinstance(nodo, NavigableString) else str(nodo)
    return _restaurar_svg(res) if "<svg" in res.lower() else res


def desenvolver_contenedores(cuerpo_html):
    aperturas, cierres = [], []
    while True:
        soup = BeautifulSoup(f"<body>{cuerpo_html}</body>", "html.parser")
        hijos = [n for n in soup.body.children
                 if not (isinstance(n, NavigableString) and not n.strip())]
        if len(hijos) == 1 and getattr(hijos[0], "name", None) in ("div", "section", "article", "main"):
            cont = hijos[0]
            aperturas.append(re.match(r"<[^>]+>", str(cont)).group(0))
            cierres.insert(0, f"</{cont.name}>")
            cuerpo_html = "".join(_nodo_a_html(n) for n in cont.children)
        else:
            return "".join(aperturas), cuerpo_html, "".join(cierres)


# ==============================================================================
# CONEXIÓN REST CON GEMINI
# ==============================================================================
def crear_contexto_ssl(desactivar_ssl: bool = False):
    if desactivar_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()

def obtener_modelos_disponibles_api(api_key: str, desactivar_ssl: bool = False) -> tuple[list[str], list[str]]:
    ctx = crear_contexto_ssl(desactivar_ssl)
    excluir = ("tts", "embedding", "live", "audio", "robotics", "computer-use")
    modelos_texto, modelos_img = [], []
    token = ""
    try:
        while True:
            url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
            if token:
                url += "&pageToken=" + urllib.parse.quote(token)
            req = urllib.request.Request(url, headers={"x-goog-api-key": api_key})
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for m in data.get("models", []):
                nombre = m.get("name", "").replace("models/", "")
                bajo = nombre.lower()
                if "generateContent" not in m.get("supportedGenerationMethods", []):
                    continue
                if not bajo.startswith("gemini"):
                    continue
                if "image" in bajo or "banana" in bajo:
                    if nombre not in modelos_img:
                        modelos_img.append(nombre)
                elif not any(x in bajo for x in excluir):
                    if nombre not in modelos_texto:
                        modelos_texto.append(nombre)
            token = data.get("nextPageToken", "")
            if not token:
                break
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:200]}")
    except Exception as e:
        raise RuntimeError(f"No se pudo consultar la lista de modelos: {e}")

    if not modelos_texto and not modelos_img:
        raise RuntimeError("La API no devolvió modelos utilizables.")
    return sorted(modelos_texto, reverse=True), sorted(modelos_img, reverse=True)

def llamar_gemini_rest(
    api_key: str,
    modelo: str,
    prompt_sistema: str,
    contenido_usuario: str,
    aviso_cb=None,
    detener_cb=None,
    sin_reparar: bool = False,
    desactivar_ssl: bool = False,
    timeout_seg: int = 240,
    max_intentos: int = 5
) -> str:
    url = URL_API.format(modelo=modelo)

    instruccion_final = prompt_sistema
    if prompt_sistema and not sin_reparar:
        instruccion_final += (
            "\n\nREGLA TÉCNICA ESTRICTA: Conserva intactas todas las etiquetas XHTML, sus atributos "
            "(id, class, href, src), las imágenes <img> y los bloques <code>, <pre> y <script>. "
            "Traduce solo el texto visible. Prohibido usar asteriscos Markdown (*texto* o **texto**); "
            "usa únicamente <em> y <strong>."
        )

    config_gen = _config_generacion(modelo)

    def _armar_datos():
        payload = {
            "contents": [{"parts": [{"text": contenido_usuario}]}],
            "safetySettings": AJUSTES_SEGURIDAD,
            "generationConfig": config_gen,
        }
        if instruccion_final:
            payload["systemInstruction"] = {"parts": [{"text": instruccion_final}]}
        return json.dumps(payload).encode("utf-8")

    datos_json = _armar_datos()
    ctx = crear_contexto_ssl(desactivar_ssl)

    for intento in range(max_intentos):
        if detener_cb and detener_cb():
            raise InterruptedError("Traducción pausada por el usuario.")

        req = urllib.request.Request(
            url,
            data=datos_json,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key}
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout_seg, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            _registrar_uso(data)

            candidatos = data.get("candidates", [])
            if not candidatos:
                bloqueo = data.get("promptFeedback", {}).get("blockReason", "SIN_CANDIDATOS")
                raise BloqueRechazado(f"Respuesta bloqueada por filtro: {bloqueo}")

            cand = candidatos[0]
            finish_reason = cand.get("finishReason", "STOP")
            if finish_reason == "MAX_TOKENS":
                raise RespuestaTruncada("La respuesta excedió el límite de tokens de salida.")
            if finish_reason in ("RECITATION", "SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
                raise BloqueRechazado(f"Generación bloqueada por política: {finish_reason}")

            partes = cand.get("content", {}).get("parts", [])
            texto = "".join(p.get("text", "") for p in partes if not p.get("thought"))
            if not texto.strip():
                raise ValueError("Respuesta vacía del modelo.")

            return quitar_cercas_codigo(texto) if sin_reparar else limpiar_bloques_markdown(texto)

        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", errors="ignore")
            if e.code == 429:
                if "PerDay" in cuerpo or "daily" in cuerpo.lower():
                    raise ErrorNoRecuperable(f"Cuota diaria agotada: {cuerpo[:300]}")
                if intento == max_intentos - 1:
                    raise ErrorCapitulo(f"Error 429 persistente: {cuerpo[:300]}")
                for seg in range(65, 0, -5):
                    if detener_cb and detener_cb():
                        raise InterruptedError("Traducción pausada por el usuario.")
                    if aviso_cb:
                        aviso_cb(f"⏳ Cuota alcanzada (429). Reanudando en {seg}s...")
                    pausa(5, detener_cb)
                continue
            if e.code in (401, 403, 404) or (e.code == 400 and re.search(r"API key|API_KEY", cuerpo)):
                raise ErrorNoRecuperable(f"HTTP {e.code}: {cuerpo[:500]}")
            if e.code == 400:
                if "thinking" in cuerpo.lower() and "thinkingConfig" in config_gen:
                    config_gen.pop("thinkingConfig", None)   # modelo sin soporte: reintenta sin thinkingConfig
                    datos_json = _armar_datos()
                    continue
                raise ErrorCapitulo(f"HTTP 400: {cuerpo[:500]}")
            if intento == max_intentos - 1:
                raise ErrorCapitulo(f"HTTP {e.code}: {cuerpo[:500]}")
            espera_exp = min(10 * (2 ** intento), 60)
            if aviso_cb:
                aviso_cb(f"⚠️ Error servidor ({e.code}). Reintentando en {espera_exp}s...")
            pausa(espera_exp, detener_cb)

        except (InterruptedError, ErrorNoRecuperable, ErrorCapitulo):
            raise

        except (socket.timeout, TimeoutError):
            if intento >= 1:
                raise ErrorCapitulo(f"Tiempo de espera agotado ({timeout_seg}s) en dos intentos.")
            if aviso_cb:
                aviso_cb(f"⚠️ Tiempo de espera agotado ({timeout_seg}s). Reintentando una vez...")
            pausa(8, detener_cb)

        except urllib.error.URLError as e:
            razon = getattr(e, "reason", None)
            if isinstance(razon, ssl.SSLError):
                raise ErrorNoRecuperable(
                    "Falló la verificación del certificado SSL. Revisa la fecha del equipo, "
                    "el antivirus o proxy, o activa la opción 'Desactivar verificación SSL' en la pestaña 4."
                )
            limite = 2 if isinstance(razon, (socket.timeout, TimeoutError)) else max_intentos
            if intento >= limite - 1:
                raise ErrorCapitulo(f"Error de red persistente: {e}")
            if aviso_cb:
                aviso_cb(f"⚠️ Error de red ({intento+1}/{limite}): {e}")
            pausa(8, detener_cb)

        except Exception as e:
            if intento >= 1:   # respuesta vacía / JSON inválido: máximo 2 intentos pagados
                raise ErrorCapitulo(f"Error inesperado: {e}")
            if aviso_cb:
                aviso_cb(f"⚠️ Reintentando bloque (2/2): {e}")
            pausa(8, detener_cb)

    raise RuntimeError("No se obtuvo respuesta válida tras varios intentos.")


def traducir_y_validar_bloque(
    api_key: str,
    modelo: str,
    prompt_sistema: str,
    bloque_orig: str,
    aviso_cb=None,
    detener_cb=None,
    desactivar_ssl: bool = False
) -> tuple[str, str]:
    prompt_actual = prompt_sistema
    ultimo_trad = ""
    motivo_fallo = ""

    for intento_val in range(2):
        ultimo_trad = llamar_gemini_rest(
            api_key, modelo, prompt_actual, bloque_orig,
            aviso_cb=aviso_cb, detener_cb=detener_cb, sin_reparar=False, desactivar_ssl=desactivar_ssl
        )
        motivo_fallo = validar_bloque(bloque_orig, ultimo_trad)
        if not motivo_fallo:
            return ultimo_trad, ""
        if intento_val == 1:
            break
        if aviso_cb:
            aviso_cb(f"🔍 Corrigiendo estructura ({motivo_fallo}) - reintento unico...")
        prompt_actual = (
            prompt_sistema +
            f"\n\nATENCIÓN CRÍTICA: En el intento previo ocurrió este error estructural: {motivo_fallo}. "
            "Debes devolver exactamente las mismas etiquetas <img>, los mismos atributos id y href, "
            "y mantener la correspondencia de párrafos <p> del original."
        )
        pausa(3, detener_cb)

    if motivo_fallo and aviso_cb:
        aviso_cb(f"⚠️ Advertencia estructural persistente: {motivo_fallo}")
    return ultimo_trad, motivo_fallo


# ==============================================================================
# LECTURA DEL ORDEN REAL (<SPINE>) Y DIAGNÓSTICO DE CAPÍTULOS
# ==============================================================================
def obtener_archivos_xhtml_en_orden_spine(z: zipfile.ZipFile) -> list[str]:
    todos_xhtml = [f for f in z.namelist() if f.lower().endswith(('.xhtml', '.html', '.htm'))]
    archivos_opf = [f for f in z.namelist() if f.lower().endswith('.opf')]
    if not archivos_opf:
        return todos_xhtml

    try:
        opf_path = archivos_opf[0]
        opf_dir = os.path.dirname(opf_path)
        tree = ET.fromstring(z.read(opf_path))

        manifest = {}
        for elem in tree.iter():
            if elem.tag.endswith('item'):
                item_id = elem.attrib.get('id')
                href = urllib.parse.unquote(elem.attrib.get('href', ''))
                if item_id and href:
                    ruta_completa = os.path.normpath(os.path.join(opf_dir, href)).replace('\\', '/')
                    manifest[item_id] = ruta_completa

        orden_spine = []
        for elem in tree.iter():
            if elem.tag.endswith('itemref'):
                idref = elem.attrib.get('idref')
                if idref in manifest:
                    ruta = manifest[idref]
                    if ruta in todos_xhtml and ruta not in orden_spine:
                        orden_spine.append(ruta)

        for f in todos_xhtml:
            if f not in orden_spine:
                orden_spine.append(f)

        return orden_spine
    except Exception:
        return todos_xhtml


def diagnosticar_capitulos_epub(ruta_epub: str) -> list[dict]:
    progreso = cargar_progreso_disco(ruta_epub)
    parciales = progreso.get("__parcial__", {})
    advertencias = progreso.get("__advertencias__", {})
    resultado = []

    with zipfile.ZipFile(ruta_epub, 'r') as z:
        archivos = obtener_archivos_xhtml_en_orden_spine(z)

        for nombre in archivos:
            contenido_orig = _decodificar_xhtml_seguro(z.read(nombre))
            soup_orig = BeautifulSoup(contenido_orig, "html.parser")
            texto_orig = soup_orig.get_text(separator=" ", strip=True)
            palabras_orig = len(texto_orig.split())

            if palabras_orig < 15:
                continue

            estado = "⏳ Pendiente"
            palabras_trad = 0
            seleccionar_por_defecto = True

            if nombre in progreso and isinstance(progreso[nombre], str):
                contenido_trad = progreso[nombre]
                soup_trad = BeautifulSoup(contenido_trad, "html.parser")
                texto_trad = soup_trad.get_text(separator=" ", strip=True)
                palabras_trad = len(texto_trad.split())

                ratio = (palabras_trad / palabras_orig) if palabras_orig > 0 else 1.0
                tiene_cierre = ("</body>" in contenido_trad.lower()) or ("</html>" in contenido_trad.lower())

                cuerpo_orig_m = re.search(r'<body[^>]*>(.*?)</body>', contenido_orig, re.DOTALL | re.IGNORECASE)
                cuerpo_trad_m = re.search(r'<body[^>]*>(.*?)</body>', contenido_trad, re.DOTALL | re.IGNORECASE)
                alerta_estructura = advertencias.get(nombre, "")
                if not alerta_estructura and cuerpo_orig_m and cuerpo_trad_m:
                    alerta_estructura = validar_bloque(cuerpo_orig_m.group(1), cuerpo_trad_m.group(1))

                if texto_trad.strip() == texto_orig.strip():
                    estado = "❌ Sin traducir (en inglés)"
                    seleccionar_por_defecto = True
                elif not tiene_cierre or (palabras_orig > 120 and ratio < 0.68):
                    porcentaje = int(ratio * 100)
                    estado = f"⚠️ Incompleto / A medias ({porcentaje}%)"
                    seleccionar_por_defecto = True
                elif alerta_estructura:
                    porcentaje = int(ratio * 100)
                    estado = f"🔍 Revisar estructura ({alerta_estructura})"
                    seleccionar_por_defecto = False
                else:
                    porcentaje = int(ratio * 100)
                    estado = f"✅ Traducido ({porcentaje}%)"
                    seleccionar_por_defecto = False

            elif nombre in parciales and isinstance(parciales[nombre], dict):
                bloques_guardados = len(parciales[nombre])
                estado = f"⏸️ En progreso ({bloques_guardados} bloques listos)"
                seleccionar_por_defecto = True

            resultado.append({
                "nombre": nombre,
                "palabras_orig": palabras_orig,
                "palabras_trad": palabras_trad,
                "estado": estado,
                "seleccionar": seleccionar_por_defecto
            })

    return resultado


# ==============================================================================
# DIVISIÓN POR NODOS DOM Y TRADUCCIÓN CON SUBDIVISIÓN ANTE MAX_TOKENS Y RECHAZOS
# ==============================================================================
def dividir_cuerpo_xhtml(cuerpo_html: str, max_palabras: int = 1500) -> list[str]:
    soup = BeautifulSoup(f"<body>{cuerpo_html}</body>", "html.parser")
    body = soup.body
    if not body:
        return [cuerpo_html]

    nodos = list(body.children)
    bloques = []
    actual = []
    palabras_actual = 0

    for nodo in nodos:
        html_nodo = _nodo_a_html(nodo)
        if isinstance(nodo, NavigableString):
            palabras_nodo = len(str(nodo).split())
        else:
            palabras_nodo = len(nodo.get_text(" ", strip=True).split())

        if palabras_actual + palabras_nodo > max_palabras and actual:
            bloques.append("".join(actual))
            actual = [html_nodo]
            palabras_actual = palabras_nodo
        else:
            actual.append(html_nodo)
            palabras_actual += palabras_nodo

    if actual:
        bloques.append("".join(actual))

    return bloques if bloques else [cuerpo_html]


def _limpiar_envoltura_body(html_fragmento: str) -> str:
    limpio = re.sub(r'^.*?<body[^>]*>', '', html_fragmento, flags=re.DOTALL | re.IGNORECASE)
    limpio = re.sub(r'</body>.*$', '', limpio, flags=re.DOTALL | re.IGNORECASE)
    return reparar_markdown_residual(limpio)


def _dividir_en_dos(bloque_html):
    soup = BeautifulSoup(f"<body>{bloque_html}</body>", "html.parser")
    nodos = [_nodo_a_html(n) for n in soup.body.children]
    if len([n for n in nodos if n.strip()]) < 2:
        return []        # un solo nodo: dividirlo rompería etiquetas
    pesos = [len(BeautifulSoup(n, "html.parser").get_text(" ").split()) for n in nodos]
    mitad, acum, corte = sum(pesos) / 2, 0, 1
    for i, w in enumerate(pesos[:-1], 1):
        acum += w
        corte = i
        if acum >= mitad:
            break
    partes = ["".join(nodos[:corte]), "".join(nodos[corte:])]
    return [p for p in partes if p.strip()]


def _traducir_bloque_con_subdivision(
    api_key: str,
    modelo: str,
    instruccion: str,
    bloque_html: str,
    pausa_seg: int,
    aviso_cb=None,
    detener_cb=None,
    desactivar_ssl: bool = False,
    profundidad: int = 0
) -> tuple[str, list[str]]:
    try:
        trad, adv = traducir_y_validar_bloque(
            api_key, modelo, instruccion, bloque_html,
            aviso_cb=aviso_cb, detener_cb=detener_cb, desactivar_ssl=desactivar_ssl
        )
        return _limpiar_envoltura_body(trad), ([adv] if adv else [])
    except (RespuestaTruncada, BloqueRechazado) as e:
        limite = 3 if isinstance(e, RespuestaTruncada) else 2
        sub_bloques = _dividir_en_dos(bloque_html) if profundidad < limite else []
        if len(sub_bloques) < 2:
            if isinstance(e, RespuestaTruncada):
                raise ErrorCapitulo(f"No se pudo subdividir más el bloque: {e}")
            if aviso_cb:
                aviso_cb(f"⚠️ Bloque conservado en inglés: {e}")
            return bloque_html, [f"Bloque sin traducir ({e})"]
        if aviso_cb:
            aviso_cb(f"✂️ Subdividiendo bloque en {len(sub_bloques)} partes...")
        partes, advs = [], []
        for i, sub in enumerate(sub_bloques, 1):
            t, a = _traducir_bloque_con_subdivision(
                api_key, modelo, instruccion, sub, pausa_seg,
                aviso_cb, detener_cb, desactivar_ssl, profundidad + 1
            )
            partes.append(t)
            advs.extend(a)
            if i < len(sub_bloques):
                pausa(pausa_seg, detener_cb)
        return "\n".join(partes), advs


def traducir_xhtml_completo_o_por_partes(
    api_key: str,
    modelo: str,
    guia: str,
    contenido_xhtml: str,
    palabras_totales: int,
    pausa_seg: int,
    aviso_cb=None,
    detener_cb=None,
    desactivar_ssl: bool = False,
    ruta_epub: str = "",
    nombre_archivo: str = ""
) -> tuple[str, str]:
    patron_body = re.compile(r'(<body[^>]*>)(.*?)(</body>)', re.DOTALL | re.IGNORECASE)
    match = patron_body.search(contenido_xhtml)

    if not match:
        res, adv = traducir_y_validar_bloque(
            api_key, modelo, guia, contenido_xhtml,
            aviso_cb=aviso_cb, detener_cb=detener_cb, desactivar_ssl=desactivar_ssl
        )
        return actualizar_idioma_xhtml(reparar_markdown_residual(res)), adv

    inicio_doc = actualizar_idioma_xhtml(contenido_xhtml[:match.start(2)])
    cuerpo_original = match.group(2)
    fin_doc = contenido_xhtml[match.end(2):]

    apertura, interior, cierre = desenvolver_contenedores(cuerpo_original)
    bloques = dividir_cuerpo_xhtml(interior, max_palabras=PALABRAS_POR_BLOQUE)
    total_partes = len(bloques)

    progreso = cargar_progreso_disco(ruta_epub) if ruta_epub else {}
    parciales_libro = progreso.setdefault("__parcial__", {})
    bloques_guardados = parciales_libro.setdefault(nombre_archivo, {}) if nombre_archivo else {}

    instruccion_fragmento = (
        guia + "\n\nIMPORTANTE: Recibes un fragmento interno del <body> de un capítulo XHTML. "
        "Tradúcelo completo sin agregar etiquetas <html> ni <body>."
    )

    bloques_traducidos = []
    advertencias_cap = []

    for idx_parte, bloque in enumerate(bloques, start=1):
        if detener_cb and detener_cb():
            raise InterruptedError("Traducción pausada por el usuario.")

        h_bloque = hashlib.sha1(bloque.encode("utf-8")).hexdigest()[:10]
        clave_bloque = h_bloque

        if clave_bloque in bloques_guardados and bloques_guardados[clave_bloque].strip():
            if aviso_cb:
                aviso_cb(f"Reanudando bloque {idx_parte}/{total_partes} desde caché...")
            bloques_traducidos.append(bloques_guardados[clave_bloque])
            continue

        texto_bloque = re.sub(r'<[^>]+>', ' ', bloque).strip()
        if len(texto_bloque.split()) < 3:
            bloques_traducidos.append(bloque)
            continue

        if aviso_cb:
            aviso_cb(f"Traduciendo parte {idx_parte}/{total_partes} del capítulo...")

        def cb_sub(msg):
            if aviso_cb:
                aviso_cb(f"[Parte {idx_parte}/{total_partes}] {msg}")

        trad_parte, advs = _traducir_bloque_con_subdivision(
            api_key, modelo, instruccion_fragmento, bloque, pausa_seg,
            aviso_cb=cb_sub, detener_cb=detener_cb, desactivar_ssl=desactivar_ssl
        )
        advertencias_cap.extend(advs)
        bloques_traducidos.append(trad_parte)

        if ruta_epub and nombre_archivo and not any("sin traducir" in a for a in advs):
            bloques_guardados[clave_bloque] = trad_parte
            guardar_progreso_disco(ruta_epub, progreso)

        if idx_parte < total_partes:
            pausa(pausa_seg, detener_cb)

    m_title = re.search(r'(<title[^>]*>)(.*?)(</title>)', inicio_doc, flags=re.DOTALL | re.IGNORECASE)
    if m_title and len(m_title.group(2).strip()) > 1:
        import html as _html
        from xml.sax.saxutils import escape
        titulo_orig = _html.unescape(m_title.group(2)).strip()
        if not titulo_orig.isdigit():
            try:
                titulos_cache = progreso.setdefault("__titulos__", {})
                t_trad = titulos_cache.get(titulo_orig)
                if not t_trad:
                    t_trad = _titulo_desde_encabezado(cuerpo_original, "\n".join(bloques_traducidos), titulo_orig)
                if not t_trad:
                    t_trad = llamar_gemini_rest(
                        api_key, modelo,
                        "Traduce únicamente este título de capítulo al español neutro, sin comillas ni explicaciones.",
                        titulo_orig, detener_cb=detener_cb, sin_reparar=True, desactivar_ssl=desactivar_ssl
                    )
                    t_trad = t_trad.strip()
                if t_trad and titulo_orig not in titulos_cache:
                    titulos_cache[titulo_orig] = t_trad
                    if ruta_epub:
                        guardar_progreso_disco(ruta_epub, progreso)

                if t_trad:
                    inicio_doc = (
                        inicio_doc[:m_title.start(2)] +
                        escape(t_trad) +
                        inicio_doc[m_title.end(2):]
                    )
            except (InterruptedError, ErrorNoRecuperable):
                raise
            except Exception:
                pass

    doc_final = inicio_doc + apertura + "\n".join(bloques_traducidos) + cierre + fin_doc
    resumen_adv = "; ".join(dict.fromkeys(advertencias_cap))
    return doc_final, resumen_adv


# ==============================================================================
# GESTIÓN DE EPUB, GUÍAS E IMÁGENES
# ==============================================================================
def convertir_al_formato_original(bytes_nueva: bytes, bytes_orig: bytes) -> bytes:
    if not TIENE_PIL:
        return bytes_nueva
    try:
        img_orig = Image.open(BytesIO(bytes_orig))
        fmt = img_orig.format or "JPEG"
        
        img_nueva = Image.open(BytesIO(bytes_nueva))
        if img_nueva.format == fmt:
            return bytes_nueva

        if fmt == "JPEG" and img_nueva.mode in ("RGBA", "P", "LA"):
            img_nueva = img_nueva.convert("RGB")
        out = BytesIO()
        img_nueva.save(out, format=fmt, **({"quality": 92} if fmt == "JPEG" else {}))
        return out.getvalue()
    except Exception:
        return bytes_nueva


def extraer_texto_epub(ruta_epub: str) -> str:
    textos = []
    with zipfile.ZipFile(ruta_epub, 'r') as z:
        for nombre in obtener_archivos_xhtml_en_orden_spine(z):
            contenido = _decodificar_xhtml_seguro(z.read(nombre))
            soup = BeautifulSoup(contenido, "html.parser")
            t = soup.get_text(separator=" ", strip=True)
            if len(t) > 50:
                textos.append(t)
    return "\n\n".join(textos)


def generar_guia_analisis(
    api_key: str,
    modelo: str,
    ruta_epub: str,
    tipo_libro: str,
    guia_previa: str = "",
    estado_cb=None,
    detener_cb=None,
    desactivar_ssl: bool = False
) -> str:
    texto_completo = extraer_texto_epub(ruta_epub)
    plantilla_base = DICCIONARIO_PROMPTS.get(tipo_libro, PROMPT_NOVELA_LIGERA)

    bloque_serie = ""
    if guia_previa.strip():
        bloque_serie = (
            f"IMPORTANTE - GUÍA BASE DEL VOLUMEN ANTERIOR DE LA SERIE:\n"
            f"{guia_previa.strip()}\n"
            f"Debes mantener absoluta concordancia con los nombres, tratamientos y decisiones tomadas en la guía previa, "
            f"añadiendo solo los personajes nuevos y los nuevos términos específicos de este volumen.\n"
        )

    prompt_final = plantilla_base.replace("{BLOQUE_SERIE}", bloque_serie)
    guia = llamar_gemini_rest(
        api_key, modelo, prompt_final, texto_completo,
        aviso_cb=estado_cb, detener_cb=detener_cb, sin_reparar=True,
        desactivar_ssl=desactivar_ssl,
        timeout_seg=600, max_intentos=2
    )

    if guia.strip():
        ruta_guia = obtener_ruta_guia_auto(ruta_epub)
        ruta_tmp = ruta_guia + ".tmp"
        with open(ruta_tmp, "w", encoding="utf-8") as f:
            f.write(guia.strip())
        os.replace(ruta_tmp, ruta_guia)

    return guia


def extraer_portada_epub(ruta_epub: str) -> tuple[str, bytes]:
    with zipfile.ZipFile(ruta_epub, 'r') as z:
        namelist = z.namelist()
        archivos_opf = [f for f in namelist if f.lower().endswith('.opf')]
        if archivos_opf:
            opf_path = archivos_opf[0]
            opf_dir = os.path.dirname(opf_path)
            try:
                tree = ET.fromstring(z.read(opf_path))
                meta_cover_id = None
                for elem in tree.iter():
                    if elem.tag.endswith('meta') and elem.attrib.get('name', '').lower() == 'cover':
                        meta_cover_id = elem.attrib.get('content', '').strip()
                        break

                candidato_por_id = None
                for elem in tree.iter():
                    if elem.tag.endswith('item'):
                        props = elem.attrib.get('properties', '').split()
                        item_id = elem.attrib.get('id', '')
                        media = elem.attrib.get('media-type', '')
                        href = urllib.parse.unquote(elem.attrib.get('href', ''))
                        if not href or not media.startswith('image/'):
                            continue
                        ruta_img = os.path.normpath(os.path.join(opf_dir, href)).replace('\\', '/')
                        if ruta_img not in namelist:
                            continue

                        if 'cover-image' in props or (meta_cover_id and item_id == meta_cover_id):
                            return ruta_img, z.read(ruta_img)
                        if item_id.lower() in ('cover', 'cover-image', 'img-cover'):
                            candidato_por_id = ruta_img

                if candidato_por_id:
                    return candidato_por_id, z.read(candidato_por_id)
            except Exception:
                pass

        patron_cover = re.compile(r'(?:^|/|[_-])cover(?:[_-]\d+)?\.(?:jpe?g|png|webp)$', re.IGNORECASE)
        for f in namelist:
            if patron_cover.search(f) and not re.search(r'(?:back|rear)', f, re.IGNORECASE):
                return f, z.read(f)

    return "", b""


def listar_imagenes_epub(ruta_epub: str) -> list[dict]:
    nombre_portada, _ = extraer_portada_epub(ruta_epub)
    carpeta_trad = obtener_carpeta_imagenes_traducidas(ruta_epub, crear=False)
    imagenes = []

    with zipfile.ZipFile(ruta_epub, 'r') as z:
        for info in z.infolist():
            if info.filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                es_portada = (info.filename == nombre_portada)
                archivo_local = os.path.join(carpeta_trad, info.filename.replace("/", "__"))
                traducida = os.path.exists(archivo_local)
                imagenes.append({
                    "nombre": info.filename,
                    "tamano_kb": round(info.file_size / 1024, 1),
                    "es_portada": es_portada,
                    "traducida": traducida
                })
    return imagenes


def leer_bytes_imagen_epub(ruta_epub: str, nombre_interno: str, preferir_traducida: bool = True) -> bytes:
    if preferir_traducida:
        carpeta_trad = obtener_carpeta_imagenes_traducidas(ruta_epub, crear=False)
        archivo_local = os.path.join(carpeta_trad, nombre_interno.replace("/", "__"))
        if os.path.exists(archivo_local):
            with open(archivo_local, "rb") as f:
                return f.read()

    with zipfile.ZipFile(ruta_epub, 'r') as z:
        return z.read(nombre_interno)


def guardar_imagen_traducida_disco(ruta_epub: str, nombre_interno: str, bytes_img: bytes) -> str:
    with zipfile.ZipFile(ruta_epub, 'r') as z:
        bytes_orig = z.read(nombre_interno)
    bytes_convertidos = convertir_al_formato_original(bytes_img, bytes_orig)

    carpeta_trad = obtener_carpeta_imagenes_traducidas(ruta_epub, crear=True)
    archivo_local = os.path.join(carpeta_trad, nombre_interno.replace("/", "__"))
    archivo_tmp = archivo_local + ".tmp"
    with open(archivo_tmp, "wb") as f:
        f.write(bytes_convertidos)
    os.replace(archivo_tmp, archivo_local)
    return archivo_local


def traducir_imagen_con_gemini_api(
    api_key: str,
    bytes_imagen: bytes,
    ext: str,
    prompt_edicion: str,
    modelo_img: str,
    detener_cb=None,
    desactivar_ssl: bool = False
) -> bytes:
    if not modelo_img:
        raise RuntimeError("No hay modelo de imagen seleccionado. Actualiza la lista de modelos en la pestaña 4.")

    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    mime_type = mime_map.get(ext.lower(), "image/jpeg")
    b64_input = base64.b64encode(bytes_imagen).decode("utf-8")

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt_edicion},
                {"inlineData": {"mimeType": mime_type, "data": b64_input}}
            ]
        }],
        "safetySettings": AJUSTES_SEGURIDAD,
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "temperature": 0.2}
    }
    req = urllib.request.Request(
        URL_API.format(modelo=modelo_img),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key}
    )
    ctx = crear_contexto_ssl(desactivar_ssl)
    ultimo_error = ""

    for intento in range(3):
        if detener_cb and detener_cb():
            raise InterruptedError("Operación cancelada por el usuario.")
        try:
            with urllib.request.urlopen(req, timeout=180, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            _registrar_uso(data)

            textos = []
            candidatos = data.get("candidates", [])
            if candidatos:
                for parte in candidatos[0].get("content", {}).get("parts", []):
                    if parte.get("thought", False):
                        continue
                    if "inlineData" in parte and "data" in parte["inlineData"]:
                        return base64.b64decode(parte["inlineData"]["data"])
                    if parte.get("text"):
                        textos.append(parte["text"])
            ultimo_error = f"El modelo {modelo_img} respondió sin imagen. {' '.join(textos)[:200]}"
            break   # respuesta válida sin imagen: reintentar solo gastaría más
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", errors="ignore")
            if e.code in (401, 403, 429):
                raise ErrorNoRecuperable(f"HTTP {e.code} ({modelo_img}): {cuerpo[:300]}")
            if e.code >= 500 and intento < 2:
                pausa(5, detener_cb)
                continue
            ultimo_error = f"HTTP {e.code} ({modelo_img}): {cuerpo[:250]}"
            break
        except Exception as e:
            ultimo_error = str(e)
            break

    raise RuntimeError(
        f"No se pudo generar la imagen vía API ({ultimo_error}).\n\n"
        "Puedes usar los botones inferiores para exportarla a Gemini Web e importarla aquí."
    )

# ==============================================================================
# TRADUCCIÓN SELECTIVA TOLERANTE A FALLOS Y COMPILACIÓN
# ==============================================================================
def traducir_capitulos_seleccionados(
    progreso_cb,
    detener_cb,
    api_key: str,
    modelo: str,
    ruta_epub: str,
    guia: str,
    pausa_seg: int,
    archivos_seleccionados: list[str],
    desactivar_ssl: bool = False
) -> list[tuple[str, str]]:
    reiniciar_uso()
    total = len(archivos_seleccionados)
    errores = []
    fallos_seguidos = 0

    with zipfile.ZipFile(ruta_epub, 'r') as zin:
        for idx, nombre in enumerate(archivos_seleccionados, start=1):
            nombre_corto = os.path.basename(nombre)
            try:
                if detener_cb and detener_cb():
                    progreso_cb(idx - 1, total, "⏹️ Proceso detenido de forma segura por el usuario.")
                    break
                if TOPE_TOKENS and (USO["entrada"] + USO["salida"] + USO["pensamiento"]) > TOPE_TOKENS:
                    raise ErrorNoRecuperable(f"Se alcanzó el tope de tokens configurado. {resumen_uso()}")

                contenido_orig = _decodificar_xhtml_seguro(zin.read(nombre))
                palabras = contar_palabras(contenido_orig)

                progreso_cb(idx, total, f"Traduciendo {nombre_corto} (~{palabras} palabras)...")

                def informar_subestado(msg):
                    progreso_cb(idx, total, f"{nombre_corto}: {msg}")

                traducido, adv = traducir_xhtml_completo_o_por_partes(
                    api_key, modelo, guia, contenido_orig, palabras, pausa_seg,
                    aviso_cb=informar_subestado, detener_cb=detener_cb,
                    desactivar_ssl=desactivar_ssl, ruta_epub=ruta_epub, nombre_archivo=nombre
                )

                if traducido and traducido.strip() != contenido_orig.strip():
                    progreso = cargar_progreso_disco(ruta_epub)
                    progreso[nombre] = reparar_markdown_residual(traducido)
                    if "__parcial__" in progreso and nombre in progreso["__parcial__"]:
                        del progreso["__parcial__"][nombre]
                    if adv:
                        progreso.setdefault("__advertencias__", {})[nombre] = adv
                    elif "__advertencias__" in progreso and nombre in progreso["__advertencias__"]:
                        del progreso["__advertencias__"][nombre]

                    guardar_progreso_disco(ruta_epub, progreso)
                    progreso_cb(idx, total, f"✓ {nombre_corto} completado. {resumen_uso()}")
                    fallos_seguidos = 0
                else:
                    errores.append((nombre_corto, "El modelo devolvió el texto sin traducir."))
                    progreso_cb(idx, total, f"✗ {nombre_corto} falló: respuesta idéntica al original.")
                    fallos_seguidos += 1
                    if fallos_seguidos >= 3:
                        raise ErrorNoRecuperable("Fallaron 3 capítulos seguidos; posible problema de cuota o red.")

                if idx < total:
                    pausa(pausa_seg, detener_cb)

            except InterruptedError:
                progreso_cb(idx - 1, total, "⏹️ Proceso detenido de forma segura por el usuario.")
                break
            except ErrorNoRecuperable:
                raise
            except Exception as e:
                errores.append((nombre_corto, str(e)))
                progreso_cb(idx, total, f"✗ {nombre_corto} falló: {e}")
                fallos_seguidos += 1
                if fallos_seguidos >= 3:
                    raise ErrorNoRecuperable(f"Fallaron 3 capítulos seguidos; último error: {e}")

    return errores


def compilar_epub_traducido(ruta_epub: str, ruta_salida: str, ruta_nueva_portada: str = "") -> tuple[int, int, str]:
    progreso = cargar_progreso_disco(ruta_epub)
    portada_interna, _ = extraer_portada_epub(ruta_epub)
    carpeta_imgs = obtener_carpeta_imagenes_traducidas(ruta_epub, crear=False)
    traducidos = totales = 0
    portada_usada = ruta_nueva_portada
    progreso_modificado = False

    with zipfile.ZipFile(ruta_epub, "r") as zcheck:
        for nom in zcheck.namelist():
            if nom.lower().endswith((".xhtml", ".html", ".htm")):
                html_o = _decodificar_xhtml_seguro(zcheck.read(nom))
                if contar_palabras(html_o) >= 15:
                    totales += 1
                    if isinstance(progreso.get(nom), str) and progreso.get(nom, "").strip():
                        traducidos += 1

    libro_completo_en_espanol = (totales > 0 and traducidos == totales)
    ruta_salida_tmp = ruta_salida + ".tmp"

    try:
        with zipfile.ZipFile(ruta_epub, "r") as zin, \
             zipfile.ZipFile(ruta_salida_tmp, "w", zipfile.ZIP_DEFLATED) as zout:

            if "mimetype" in zin.namelist():
                zout.writestr("mimetype", zin.read("mimetype"), compress_type=zipfile.ZIP_STORED)

            for item in zin.infolist():
                nombre = item.filename
                if nombre == "mimetype":
                    continue

                datos = zin.read(nombre)

                if nombre.lower().endswith((".xhtml", ".html", ".htm")):
                    trad = progreso.get(nombre, "")
                    if isinstance(trad, str) and trad.strip():
                        trad_limpio = actualizar_idioma_xhtml(reparar_markdown_residual(trad))
                        if trad_limpio != trad:
                            progreso[nombre] = trad_limpio
                            progreso_modificado = True
                        datos = trad_limpio.encode("utf-8")

                elif nombre.lower().endswith(".opf"):
                    if libro_completo_en_espanol:
                        opf_texto = datos.decode("utf-8", errors="ignore")
                        datos = actualizar_idioma_opf(opf_texto).encode("utf-8")

                else:
                    local = os.path.join(carpeta_imgs, nombre.replace("/", "__"))
                    if nombre == portada_interna and ruta_nueva_portada and os.path.exists(ruta_nueva_portada):
                        with open(ruta_nueva_portada, "rb") as f:
                            bytes_nueva = f.read()
                        datos = convertir_al_formato_original(bytes_nueva, datos)
                        portada_usada = ruta_nueva_portada
                    elif os.path.exists(local):
                        with open(local, "rb") as f:
                            datos = f.read()
                        if nombre == portada_interna:
                            portada_usada = local

                zout.writestr(nombre, datos, compress_type=zipfile.ZIP_DEFLATED)

        os.replace(ruta_salida_tmp, ruta_salida)
    finally:
        if os.path.exists(ruta_salida_tmp):
            try:
                os.remove(ruta_salida_tmp)
            except OSError:
                pass

    if progreso_modificado:
        guardar_progreso_disco(ruta_epub, progreso)

    return traducidos, totales, portada_usada