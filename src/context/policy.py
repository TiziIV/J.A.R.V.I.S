"""Políticas de acceso, validación de raíces autorizadas, sanitización y solo lectura para N7.

Este módulo concentra las defensas de seguridad y privacidad del Context Awareness Engine:
1. Reutiliza `AUTHORIZED_FILE_ROOTS` y `FORBIDDEN_SYSTEM_PREFIXES` de `config/settings.py`
   para evitar políticas contradictorias con N5 (`FileEditorSkill` / `PolicyEngine`).
2. Aplica denegación por defecto (`default-deny`) ante rutas relativas, path traversal (`..`),
   rutas UNC, variables `%...%` sin expandir, dispositivos reservados de Windows (`CON`, `NUL`,
   `COM1..9`, `LPT1..9`) y enlaces simbólicos / junctions NTFS (`FILE_ATTRIBUTE_REPARSE_POINT`).
3. Bloquea la inspección contextual de archivos sensibles incluso dentro del proyecto
   (`.env`, `prompts_history.txt`, `memory_store.json`, claves privadas, tokens).
4. Sanitiza nombres de procesos, ventanas, aplicaciones y archivos para neutralizar intentos
   de inyección de prompts o caracteres de control invisibles, redactando cualquier secreto.
5. Impide explícitamente cualquier operación que no sea de observación en solo lectura.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import unicodedata
from typing import Any, Optional, Sequence

from config import settings
from src.brain.exceptions import redact_secrets
from src.context.models import (
    ALL_CONTEXT_CATEGORIES,
    ContextAccessDeniedError,
    ContextCategory,
    ContextPolicyError,
)


# Nombres de dispositivos reservados de Windows prohibidos en cualquier ruta.
WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

# Atributo nativo de Windows para detectar junctions NTFS, mount points y reparse points.
FILE_ATTRIBUTE_REPARSE_POINT: int = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# Archivos sensibles cuya existencia o metadatos detallados no deben exponerse al contexto general.
FORBIDDEN_CONTEXT_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".env.test",
        ".env.staging",
        ".envrc",
        "prompts_history.txt",
        "memory_store.json",
        "credentials",
        "credentials.json",
        "token.json",
        "auth.json",
        "secrets.json",
        "secret.json",
        "secrets.yaml",
        "secrets.yml",
        "secrets.toml",
        "secrets.ini",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".htpasswd",
        "htpasswd",
        ".pgpass",
        ".my.cnf",
        ".s3cfg",
        ".boto",
        ".git-credentials",
        ".dockerconfigjson",
        ".dockercfg",
        "kubeconfig",
        ".kubeconfig",
        "terraform.tfstate",
        "terraform.tfvars",
        "file_editor_audit.jsonl",
        "jarvis_system.log",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "authorized_keys",
        "known_hosts",
        "wallet.dat",
        "wallet.json",
        "wallet.db",
        "login data",
        "login data for account",
        "cookies",
        "web data",
        "local state",
        "key4.db",
        "logins.json",
        "cert9.db",
        "vault.db",
        "ntuser.dat",
    }
)

FORBIDDEN_CONTEXT_EXTENSIONS: tuple[str, ...] = (
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".p8",
    ".ppk",
    ".kdbx",
    ".kdb",
    ".jks",
    ".keystore",
    ".wallet",
    ".ovpn",
    ".gpg",
    ".asc",
    ".der",
    ".cer",
    ".crt",
    ".log",
    # Bases de datos locales (SQLite y similares): suelen guardar sesiones, historiales o tokens.
    ".sqlite",
    ".sqlite3",
    ".db",
    ".db3",
    ".sdb",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite-journal",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".sqlite3-journal",
    ".db-wal",
    ".db-shm",
    ".db-journal",
)

# Sufijos de respaldo/temporales que no deben permitir evadir la detección de archivos sensibles.
# Incluye las convenciones reales del proyecto: `<archivo>.<YYYYMMDD_HHMMSS_ffffff>.bak` (backups de
# FileEditorSkill) y `.<archivo>.<hex>.tmp` / `.~<archivo>.<hex>.tmp` (temporales atómicos).
_BACKUP_OR_TEMP_SUFFIXES: tuple[str, ...] = (
    ".bak",
    ".backup",
    ".bkp",
    ".bk",
    ".old",
    ".orig",
    ".tmp",
    ".temp",
    ".swp",
    ".swo",
    ".save",
    ".copy",
    "~",
)

# Segmentos finales variables que también se retiran al normalizar (timestamp N5, hex de temporales,
# contador de rotación `.1`, `.2`).
_VARIABLE_TAIL_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\.\d{8}_\d{6}(?:_\d{1,6})?(?:_[a-z0-9]+)?$"),
    re.compile(r"\.[0-9a-f]{8,32}$"),
    re.compile(r"\.\d{1,4}$"),
)
_TRAILING_UNSAFE_SUFFIX_RE: re.Pattern[str] = re.compile(
    r"(?i)(?:\.backup|\.temp|\.save|\.copy|\.orig|\.swp|\.swo|\.bak|\.bkp|\.bk|\.old|\.tmp|~|"
    r"\.\d{8}_\d{6}(?:_\d{1,6})?(?:_[a-z0-9]+)?|\.[0-9a-f]{8,32}|\.\d{1,4})+$"
)

# Nombres de directorios sensibles bloqueados en cualquier nivel para consultas N7 (F3).
FORBIDDEN_CONTEXT_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".ssh",
        ".aws",
        ".azure",
        ".kube",
        ".gnupg",
        "gnupg",
        ".password-store",
        ".docker",
        ".gcloud",
        ".terraform",
        "credentials",
        "secrets",
        "keyrings",
        "wallet",
        "wallets",
        "user data",
        # Perfiles completos de navegadores y clientes con sesiones/cookies/contraseñas.
        "opera software",
        "opera gx stable",
        "opera stable",
        "opera neon",
        "bravesoftware",
        "brave-browser",
        "vivaldi",
        "yandexbrowser",
        "chromium",
        "waterfox",
        "librewolf",
        "tor browser",
        "thunderbird",
        # Gestores de contraseñas.
        "1password",
        "bitwarden",
        "lastpass",
        "keepass",
    }
)

# Subárboles relativos o de perfil de usuario bloqueados específicamente en N7 (F3)
# sin alterar las raíces autorizadas compartidas con N5 (`FileEditorSkill`).
# Nota: `BASE_DIR/data` completo se bloquea aparte (ruta absoluta) en `ContextAccessPolicy`.
FORBIDDEN_CONTEXT_SUBTREES: tuple[str, ...] = (
    "data/goals",
    "data/transactions",
    "data/backups",
    "data/memory",
    "backups",
    "microsoft/credentials",
    "microsoft/protect",
    "microsoft/vault",
    "microsoft/crypto",
    "google/chrome",
    "microsoft/edge",
    "mozilla/firefox",
    "mozilla/thunderbird",
    "discord/local storage",
    "gcloud",
)

# Operaciones mutantes o administrativas estrictamente prohibidas en N7.
FORBIDDEN_MUTATION_OPERATIONS: frozenset[str] = frozenset(
    {
        "write",
        "modify",
        "edit",
        "create",
        "delete",
        "remove",
        "unlink",
        "rmdir",
        "rmtree",
        "replace",
        "rename",
        "move",
        "chmod",
        "chown",
        "kill",
        "terminate",
        "suspend",
        "resume",
        "nice",
        "ionice",
        "execute",
        "exec",
        "spawn",
        "launch",
        "install",
        "uninstall",
        "elevate",
    }
)

# Alias en español e inglés para categorías de contexto.
CATEGORY_ALIASES: dict[str, ContextCategory] = {
    "system": ContextCategory.SYSTEM,
    "sistema": ContextCategory.SYSTEM,
    "so": ContextCategory.SYSTEM,
    "os": ContextCategory.SYSTEM,
    "hardware": ContextCategory.HARDWARE,
    "hw": ContextCategory.HARDWARE,
    "resources": ContextCategory.RESOURCES,
    "recursos": ContextCategory.RESOURCES,
    "uso": ContextCategory.RESOURCES,
    "storage": ContextCategory.STORAGE,
    "almacenamiento": ContextCategory.STORAGE,
    "disco": ContextCategory.STORAGE,
    "discos": ContextCategory.STORAGE,
    "unidades": ContextCategory.STORAGE,
    "processes": ContextCategory.PROCESSES,
    "proceso": ContextCategory.PROCESSES,
    "procesos": ContextCategory.PROCESSES,
    "applications": ContextCategory.APPLICATIONS,
    "aplicaciones": ContextCategory.APPLICATIONS,
    "apps": ContextCategory.APPLICATIONS,
    "ventanas": ContextCategory.APPLICATIONS,
    "filesystem": ContextCategory.FILESYSTEM,
    "archivos": ContextCategory.FILESYSTEM,
    "directorios": ContextCategory.FILESYSTEM,
    "files": ContextCategory.FILESYSTEM,
}

# Se conserva por compatibilidad: caracteres de control ASCII y bidireccionales/Tags de Unicode.
_CONTROL_AND_BIDI_RE: re.Pattern[str] = re.compile(
    r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]+"
)

# Marcador literal que reemplaza cualquier fragmento de un dato externo que imite instrucciones.
_FILTERED_MARKER = "[DATO_SISTEMA_FILTRADO]"

# Verbos de anulación/evasión y sustantivos-objetivo (todo en minúsculas y SIN tildes: el texto
# se compara ya plegado, ver `_neutralize_injection`).
_OVERRIDE_VERBS = (
    r"(?:ignore|ignora|ignorar|ignoren|disregard|forget|olvida|olvidar|omite|omitir|omitan|cambia|cambiar|borra|borrar|revela|revelar|"
    r"descarta|descartar|desobedece|desobedecer|anula|anular|bypass|override|overrule|"
    r"saltea|salta|saltear|reemplaza|reemplazar|sustituye|sustituir|sobrescribe|sobrescribir|"
    r"replace|overwrite)"
)
_OVERRIDE_NOUNS = (
    r"(?:instructions?|instrucciones?|rules?|reglas?|prompts?|polic(?:y|ies)|politicas?|"
    r"constraints?|restricciones?|guidelines?|directives?|directivas?|ordenes|orden|limites|"
    r"safeguards?|salvaguardas?|guardrails?|mensajes?|messages?)"
)
_TOOL_NOUNS = (
    r"(?:herramientas?|tools?|skills?|comandos?|commands?|funcion|function|file_editor|system_power|"
    r"app_launcher|document_export|web_extraction|chain_orchestrator|policy_engine)"
)

# Cada entrada: (patrón sobre texto plegado, `to_end`). Si `to_end` es True se neutraliza desde el
# inicio de la coincidencia hasta el final de la etiqueta (carga estructurada: JSON, objetos).
_INJECTION_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    # "ignora/omite/anula/reemplaza ... instrucciones/reglas/prompt/mensaje del sistema ..."
    (
        re.compile(
            rf"\b{_OVERRIDE_VERBS}\b(?:\s+[a-z0-9_']+){{0,4}}?\s+{_OVERRIDE_NOUNS}\b"
            r"(?:\s+(?:anterior(?:es)?|previos?|previas?|previous|prior|above|del\s+sistema|"
            r"de\s+seguridad|of\s+the\s+system|de\s+arriba))*"
        ),
        False,
    ),
    # "ejecuta/invoca/llama/run/execute ... herramienta/tool/skill/comando/file_editor ..."
    (
        re.compile(
            r"\b(?:ejecuta|ejecutar|ejecuten|invoca|invocar|llama|llamar|run|execute|invoke|call)\b"
            rf"(?:\s+[a-z0-9_']+){{0,3}}?\s+{_TOOL_NOUNS}\b"
        ),
        False,
    ),
    # Cambio de rol/persona.
    (
        re.compile(
            r"\b(?:you\s+are\s+now|from\s+now\s+on|a\s+partir\s+de\s+ahora|ahora\s+eres|"
            r"actua\s+como|act\s+as\s+(?:a|an|the|if))\b"
        ),
        False,
    ),
    # Etiquetas XML/HTML que imitan delimitadores de sistema/evidencia/herramientas (apertura o cierre).
    (
        re.compile(
            r"<\s*/?\s*[a-z0-9_\-:.]*(?:system|assistant|developer|user|instruction|prompt|evidencia|"
            r"untrusted|context|tool|function|skill|polic|datos|sistema|mensaje|message|role|human)"
            r"[a-z0-9_\-:.]*(?:\s[^>]{0,200})?/?\s*>"
        ),
        False,
    ),
    (re.compile(r"\[\s*/?\s*(?:inst|sys|system|assistant|user|evidencia\w*|untrusted\w*)\s*\]"), False),
    # Tokens de control de plantillas de chat: <|system|>, <|im_start|>, <<SYS>>, "### System".
    (re.compile(r"<\|[^|>]{0,40}\|>"), False),
    (re.compile(r"<<\s*/?\s*sys\s*>>"), False),
    (re.compile(r"#{2,}\s*(?:system|instruction|assistant|user|developer|sistema|instruccion)\w*\s*:?"), False),
    # Indicadores de rol directos.
    (
        re.compile(r"\b(?:system|assistant|developer|user|human|sistema|asistente|desarrollador|usuario)\s*:"),
        False,
    ),
    # Carga estructurada: objetos JSON (simples o anidados) con claves de invocación de herramientas/acciones.
    (
        re.compile(
            r"\{[^{}]{0,240}(?:\{[^{}]{0,240}\}[^{}]{0,240})*?"
            r"[\"']?(?:action|tool|tool_name|tool_calls?|tool_use|tool_input|function|function_call|"
            r"skill|skill_name|skill_params|command|arguments|path)[\"']?\s*:"
            r"[^{}]{0,240}(?:\{[^{}]{0,240}\}[^{}]{0,240})*\}"
        ),
        False,
    ),
    (
        re.compile(
            r"[\"']?(?:tool_name|tool_calls?|tool_use|tool_input|function_call|skill_name|skill_params|"
            r"arguments)[\"']?\s*:\s*(?:\{[^{}]{0,240}\}|\[[^\[\]]{0,240}\]|[\"'][^\"']{0,120}[\"']|[a-z0-9_.\-]+)?"
        ),
        False,
    ),
)

# Categorías Unicode: `Cf` (formato: ZWSP, ZWJ, BOM, bidi, tags...) se ELIMINA para que no pueda partir
# palabras; controles/privados/no asignados/separadores de línea se convierten en espacio.
_UNICODE_DROP_CATEGORIES: frozenset[str] = frozenset({"Cf"})
_UNICODE_SPACE_CATEGORIES: frozenset[str] = frozenset({"Cc", "Cs", "Co", "Cn", "Zl", "Zp"})
_UNICODE_DROP_CODEPOINTS: frozenset[int] = frozenset(
    {0x034F, *range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0)}
)
_CYRILLIC_CONFUSABLES: dict[str, str] = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x",
    "і": "i", "ј": "j", "у": "y", "к": "k", "м": "m", "т": "t",
    "в": "b", "н": "h",
}


def _normalize_untrusted_text(text: str) -> str:
    """NFKC + eliminación de caracteres invisibles y de control (anti ZWSP/bidi/homoglifos de ancho)."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", text):
        if ord(ch) in _UNICODE_DROP_CODEPOINTS:
            continue
        category = unicodedata.category(ch)
        if category in _UNICODE_DROP_CATEGORIES:
            continue
        if category in _UNICODE_SPACE_CATEGORIES:
            out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


def _neutralize_injection(text: str) -> str:
    """Reemplaza por un marcador literal los fragmentos que imiten instrucciones, roles o JSON de herramientas.

    La detección se hace sobre una copia plegada (minúsculas, sin tildes ni marcas combinantes), pero
    el reemplazo se aplica sobre el texto original mediante un mapa de índices.
    """
    folded_chars: list[str] = []
    index_map: list[int] = []
    for idx, ch in enumerate(text):
        for part in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(part):
                continue
            normalized_part = part.translate(str.maketrans(_CYRILLIC_CONFUSABLES)).lower()
            for lowered in normalized_part:
                folded_chars.append(lowered)
                index_map.append(idx)
    if not folded_chars:
        return text
    folded = "".join(folded_chars)

    spans: list[tuple[int, int]] = []
    for pattern, to_end in _INJECTION_PATTERNS:
        for match in pattern.finditer(folded):
            if match.end() == match.start():
                continue
            start = index_map[match.start()]
            end = len(text) if to_end else index_map[match.end() - 1] + 1
            spans.append((start, end))
    if not spans:
        return text

    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.append(text[cursor:start])
        pieces.append(f" {_FILTERED_MARKER} ")
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _check_single_sensitive_basename(clean_name: str) -> bool:
    """Evalúa si un nombre base normalizado (sin sufijos ADS ni puntos finales) es sensible."""
    if not clean_name:
        return False
    if clean_name in FORBIDDEN_CONTEXT_FILENAMES:
        return True
    if clean_name in {"npmrc", "envrc", "netrc", "pypirc"}:
        return True
    if clean_name == "wallet":
        return True
    # Gestores de paquetes y configuraciones de credenciales suelen crear copias con sufijos propios.
    if clean_name.startswith((".npmrc.", ".envrc.", ".netrc.", ".pypirc.", "npmrc.", "envrc.", "netrc.", "pypirc.")):
        return True
    if clean_name.startswith("wallet.") and clean_name.endswith((".bak", ".backup", ".old", ".tmp")):
        return True
    if clean_name.startswith(".env.") or clean_name.endswith(".env"):
        if clean_name not in {".env.example", ".env.template", ".env.sample", ".env.dist"}:
            return True
    if any(clean_name.endswith(ext) for ext in FORBIDDEN_CONTEXT_EXTENSIONS):
        return True
    # Claves públicas SSH (id_rsa.pub, id_ed25519.pub, ...): exponen la identidad del usuario.
    if clean_name.startswith("id_") and clean_name.endswith(".pub"):
        return True
    if clean_name.startswith(
        ("client_secret", "service_account", "service-account", "gcloud_credentials", "firebase-adminsdk")
    ):
        return True
    if clean_name.endswith(
        (
            "_secret.json",
            "_secrets.json",
            "-secret.json",
            "-secrets.json",
            "_credentials.json",
            "_token.json",
            "_audit.jsonl",
            ".wal.jsonl",
            ".goal.json",
            ".tx.json",
        )
    ):
        return True
    return False


def _sensitive_name_variants(name: str) -> tuple[str, ...]:
    """Genera variantes conservando puntos internos de nombres ocultos."""
    variants = [name]
    if name.startswith(".~"):
        editor_variant = name[2:]
        if editor_variant:
            variants.append(editor_variant)
            stripped_hidden = editor_variant.lstrip(".")
            if stripped_hidden and stripped_hidden != editor_variant:
                variants.append(stripped_hidden)
    elif name.startswith("."):
        stripped_hidden = name.lstrip(".")
        if stripped_hidden and stripped_hidden != name:
            variants.append(stripped_hidden)
    return tuple(variants)


def is_sensitive_context_filename(filename: str) -> bool:
    """Determina si un nombre de archivo corresponde a un secreto, ADS o almacén privado (F1, F3).

    - Rechaza de forma conservadora cualquier nombre con sintaxis de Alternate Data Stream (`:`).
    - Normaliza espacios y puntos finales de Windows antes de evaluar las listas de exclusión.
    - Retira de forma ITERATIVA todos los sufijos de respaldo/temporales encadenados
      (`credentials.json.bak.tmp`, `credentials.json.tmp.bak`, `x.20260101_120000_000001.bak`, ...)
      evaluando el nombre resultante tras cada retirada.
    """
    raw_clean = (filename or "").strip().lower()
    if not raw_clean:
        return False

    # F1: Cualquier presencia de ':' en un nombre de archivo indica un Alternate Data Stream (ADS)
    # o sintaxis no permitida fuera del identificador de unidad; se rechaza conservadoramente.
    if ":" in raw_clean:
        return True

    current = raw_clean.rstrip(". ")
    if not current:
        return False

    # Una expresión con sufijos repetidos evita reprocesar el nombre entero por cada `.bak`.
    # Así el coste crece con la longitud de entrada, sin un límite fijo de iteraciones.
    normalized = _TRAILING_UNSAFE_SUFFIX_RE.sub("", current).rstrip(". ")
    return any(
        _check_single_sensitive_basename(variant)
        for variant in _sensitive_name_variants(normalized)
    )


def sanitize_untrusted_label(raw_text: Any, *, max_len: int = 160) -> str:
    """Sanitiza un texto proveniente del sistema operativo (nombre de proceso, ventana, GPU o archivo).

    Defensa COMPLEMENTARIA (no es la frontera de seguridad): el aislamiento real proviene de que los datos
    de N7 viajan como datos estructurados no confiables (`ContextUntrustedEnvelope`, bloques JSON
    delimitados) y de que la ejecución depende de `PolicyEngine`/`ChainOrchestrator`/aprobación humana.

    1. Normaliza Unicode (NFKC) y elimina invisibles (ZWSP, bidi, tags) y caracteres de control.
    2. Redacta claves API, tokens y contraseñas con formato estructural (`redact_secrets`).
    3. Neutraliza (reemplaza por un marcador literal) fragmentos que imiten instrucciones, roles,
       delimitadores de prompt o JSON de herramientas, en español e inglés, ignorando tildes/mayúsculas.
    4. Acota la longitud máxima a `max_len` caracteres.
    Límite conocido: una frase maliciosa que no coincida con ningún patrón sobrevive como texto literal
    inofensivo; por eso nunca debe interpolarse como instrucción.
    """
    if raw_text is None:
        return ""
    text = _normalize_untrusted_text(str(raw_text))
    text = redact_secrets(text)
    text = _neutralize_injection(text)
    collapsed = " ".join(text.split())
    if len(collapsed) > max_len:
        return collapsed[: max_len - 3] + "..."
    return collapsed


class ContextAccessPolicy:
    """Política central de autorización en solo lectura, raíces permitidas y límites para N7."""

    def __init__(
        self,
        *,
        authorized_roots: Optional[Sequence[str | Path]] = None,
        enabled_categories: Optional[Sequence[str | ContextCategory]] = None,
        max_processes: Optional[int] = None,
        max_applications: Optional[int] = None,
        max_windows: Optional[int] = None,
        max_dir_depth: Optional[int] = None,
        max_dir_entries: Optional[int] = None,
        minimize_for_llm: Optional[bool] = None,
        include_hostname: Optional[bool] = None,
        include_process_user: Optional[bool] = None,
        include_process_exe: Optional[bool] = None,
    ) -> None:
        # F3: `BASE_DIR/data` (memoria, objetivos, auditoría, transacciones) queda fuera de la observación
        # genérica de N7. Es una exclusión propia de N7: no altera las raíces autorizadas de N5.
        self._project_data_dir_norm: str = os.path.normcase(
            str((Path(settings.BASE_DIR) / "data").resolve())
        )
        raw_roots = (
            authorized_roots
            if authorized_roots is not None
            else settings.AUTHORIZED_FILE_ROOTS
        )
        self.authorized_roots: tuple[Path, ...] = self._normalize_roots(raw_roots)
        self.enabled_categories: frozenset[ContextCategory] = self._normalize_enabled_categories(
            enabled_categories
        )
        self.max_processes: int = max(
            1, int(max_processes if max_processes is not None else settings.CONTEXT_MAX_PROCESSES)
        )
        self.max_applications: int = max(
            1,
            int(
                max_applications
                if max_applications is not None
                else settings.CONTEXT_MAX_APPLICATIONS
            ),
        )
        self.max_windows: int = max(
            1, int(max_windows if max_windows is not None else settings.CONTEXT_MAX_WINDOWS)
        )
        self.max_dir_depth: int = max(
            1, int(max_dir_depth if max_dir_depth is not None else settings.CONTEXT_MAX_DIR_DEPTH)
        )
        self.max_dir_entries: int = max(
            5,
            int(
                max_dir_entries
                if max_dir_entries is not None
                else settings.CONTEXT_MAX_DIR_ENTRIES
            ),
        )
        self.minimize_for_llm: bool = (
            bool(minimize_for_llm)
            if minimize_for_llm is not None
            else bool(settings.CONTEXT_MINIMIZE_FOR_LLM)
        )
        self.include_hostname: bool = (
            bool(include_hostname)
            if include_hostname is not None
            else bool(settings.CONTEXT_INCLUDE_HOSTNAME)
        )
        self.include_process_user: bool = (
            bool(include_process_user)
            if include_process_user is not None
            else bool(settings.CONTEXT_INCLUDE_PROCESS_USER)
        )
        self.include_process_exe: bool = (
            bool(include_process_exe)
            if include_process_exe is not None
            else bool(settings.CONTEXT_INCLUDE_PROCESS_EXE)
        )

    @staticmethod
    def _normalize_roots(roots: Sequence[str | Path]) -> tuple[Path, ...]:
        """Normaliza y filtra raíces autorizadas descartando ubicaciones prohibidas del sistema."""
        valid: list[Path] = []
        for item in roots:
            raw_str = os.path.expandvars(str(item)).strip()
            if not raw_str or "%" in raw_str or raw_str.startswith(("\\\\", "//")):
                continue
            try:
                resolved = Path(raw_str).resolve()
                lower_str = str(resolved).lower().rstrip("\\/")
                if any(
                    lower_str == forbidden or lower_str.startswith(forbidden + "\\")
                    for forbidden in settings.FORBIDDEN_SYSTEM_PREFIXES
                ):
                    continue
                if resolved not in valid:
                    valid.append(resolved)
            except (OSError, ValueError):
                continue
        return tuple(valid)

    @staticmethod
    def _normalize_enabled_categories(
        categories: Optional[Sequence[str | ContextCategory]],
    ) -> frozenset[ContextCategory]:
        if categories is None:
            raw_seq: Sequence[str | ContextCategory] = settings.CONTEXT_ENABLED_CATEGORIES
        else:
            raw_seq = categories
        normalized: set[ContextCategory] = set()
        for item in raw_seq:
            if isinstance(item, ContextCategory):
                normalized.add(item)
                continue
            key = str(item or "").strip().lower()
            if key in CATEGORY_ALIASES:
                normalized.add(CATEGORY_ALIASES[key])
        return frozenset(normalized)

    @staticmethod
    def assert_read_only_operation(operation_name: str) -> None:
        """Verifica que la operación solicitada sea estrictamente de solo lectura."""
        clean = (operation_name or "").strip().lower()
        if clean in FORBIDDEN_MUTATION_OPERATIONS or any(
            clean.startswith(f"{verb}_") or clean.endswith(f"_{verb}")
            for verb in FORBIDDEN_MUTATION_OPERATIONS
        ):
            raise ContextAccessDeniedError(
                f"Operación '{operation_name}' bloqueada por política N7: el Context Awareness Engine es estrictamente de solo lectura."
            )

    def resolve_category(self, raw_category: str | ContextCategory) -> ContextCategory:
        """Traduce un identificador o alias de categoría a `ContextCategory` y valida que esté habilitada."""
        if isinstance(raw_category, ContextCategory):
            cat = raw_category
        else:
            key = str(raw_category or "").strip().lower()
            if key not in CATEGORY_ALIASES:
                raise ContextPolicyError(
                    f"Categoría de contexto desconocida: '{raw_category}'. "
                    f"Categorías válidas: {', '.join(c.value for c in ALL_CONTEXT_CATEGORIES)}."
                )
            cat = CATEGORY_ALIASES[key]

        if cat not in self.enabled_categories:
            raise ContextPolicyError(
                f"La categoría de contexto '{cat.value}' está deshabilitada por política de configuración."
            )
        return cat

    def validate_requested_categories(
        self,
        categories: Optional[Sequence[str | ContextCategory]] = None,
    ) -> tuple[ContextCategory, ...]:
        """Normaliza y valida la lista de categorías solicitadas preservando orden sin duplicados."""
        if categories is None:
            # Por defecto en un snapshot general sin rutas explícitas, consulta las categorías
            # de estado general habilitadas (excluyendo filesystem si no se pidieron archivos).
            default_order = (
                ContextCategory.SYSTEM,
                ContextCategory.HARDWARE,
                ContextCategory.RESOURCES,
                ContextCategory.STORAGE,
                ContextCategory.PROCESSES,
                ContextCategory.APPLICATIONS,
            )
            return tuple(cat for cat in default_order if cat in self.enabled_categories)

        if isinstance(categories, (str, bytes)) or not isinstance(categories, Sequence):
            raise ContextPolicyError(
                "El parámetro `categories` debe ser una secuencia de nombres de categorías."
            )
        if len(categories) == 0:
            raise ContextPolicyError("Se debe especificar al menos una categoría de contexto.")

        result: list[ContextCategory] = []
        for item in categories:
            cat = self.resolve_category(item)
            if cat not in result:
                result.append(cat)
        return tuple(result)

    @staticmethod
    def is_stat_reparse_or_symlink(st: Any, path_obj: Optional[Path] = None) -> bool:
        """Evalúa sobre un `stat_result` de `lstat` si corresponde a symlink, junction o reparse point (F5)."""
        try:
            mode = getattr(st, "st_mode", 0)
            if isinstance(mode, int) and stat.S_ISLNK(mode):
                return True
            file_attrs = getattr(st, "st_file_attributes", 0)
            if isinstance(file_attrs, int) and (file_attrs & FILE_ATTRIBUTE_REPARSE_POINT):
                return True
            if path_obj is not None:
                if path_obj.is_symlink():
                    return True
                if hasattr(os.path, "isjunction") and os.path.isjunction(path_obj):
                    return True
        except OSError:
            return True
        return False

    @classmethod
    def is_reparse_point_or_symlink(cls, path_obj: Path) -> bool:
        """Comprueba sin seguir el destino si una ruta es symlink, junction NTFS o reparse point."""
        try:
            if path_obj.is_symlink():
                return True
            if hasattr(os.path, "isjunction") and os.path.isjunction(path_obj):
                return True
            st = os.lstat(path_obj)
            return cls.is_stat_reparse_or_symlink(st, path_obj)
        except OSError:
            return False

    def is_forbidden_context_subpath(
        self,
        path_obj: Path,
        auth_root: Optional[Path] = None,
    ) -> bool:
        """Determina si una ruta pertenece a un directorio o subárbol sensible excluido en N7 (F3)."""
        norm_path = os.path.normcase(str(path_obj))
        if norm_path == self._project_data_dir_norm or norm_path.startswith(
            self._project_data_dir_norm + os.sep
        ):
            return True

        try:
            rel_parts = (
                path_obj.relative_to(auth_root).parts
                if auth_root is not None
                else path_obj.parts[1:]
            )
        except ValueError:
            rel_parts = path_obj.parts[1:]

        norm_parts: list[str] = []
        for raw_part in rel_parts:
            clean_part = raw_part.strip().rstrip(". ").lower()
            if not clean_part:
                continue
            norm_parts.append(clean_part)
            if clean_part in FORBIDDEN_CONTEXT_DIR_NAMES:
                return True
            if is_sensitive_context_filename(raw_part):
                return True

        rel_posix = "/".join(norm_parts)
        if rel_posix == "data" or rel_posix.startswith("data/"):
            return True
        full_posix = "/".join(
            p.strip().rstrip(". ").lower() for p in path_obj.parts[1:] if p.strip()
        )
        for subtree in FORBIDDEN_CONTEXT_SUBTREES:
            sub_low = subtree.strip("/").lower()
            if rel_posix == sub_low or rel_posix.startswith(sub_low + "/"):
                return True
            if (
                full_posix == sub_low
                or full_posix.startswith(sub_low + "/")
                or full_posix.endswith("/" + sub_low)
                or f"/{sub_low}/" in full_posix
            ):
                return True

        return False

    def _assert_path_node_safe(self, path_obj: Path) -> None:
        """Verifica con `lexists`/`lstat` que un nodo no sea symlink/junction ni lance PermissionError (F2, F5)."""
        try:
            if path_obj.is_symlink():
                raise ContextAccessDeniedError(
                    f"Ruta rechazada: se detectó un enlace simbólico en '{path_obj.name}'."
                )
            if hasattr(os.path, "isjunction") and os.path.isjunction(path_obj):
                raise ContextAccessDeniedError(
                    f"Ruta rechazada: se detectó un junction NTFS en '{path_obj.name}'."
                )
        except ContextAccessDeniedError:
            raise
        except PermissionError as exc:
            raise ContextAccessDeniedError(
                f"Acceso denegado por permisos del sistema operativo en '{path_obj.name}'."
            ) from exc
        except OSError as exc:
            raise ContextAccessDeniedError(
                f"No fue posible verificar de forma segura '{path_obj.name}' ({type(exc).__name__})."
            ) from exc

        try:
            st = os.lstat(path_obj)
        except FileNotFoundError:
            # Si lstat reporta FileNotFoundError pero lexists es True, se trata de un enlace/reparse roto.
            try:
                still_lexists = os.path.lexists(path_obj)
            except OSError as exc:
                raise ContextAccessDeniedError(
                    f"Acceso denegado al comprobar existencia de '{path_obj.name}'."
                ) from exc
            if still_lexists:
                raise ContextAccessDeniedError(
                    f"Ruta rechazada: se detectó un enlace o junction NTFS roto en '{path_obj.name}'."
                )
            return
        except PermissionError as exc:
            raise ContextAccessDeniedError(
                f"Acceso denegado por permisos del sistema operativo en '{path_obj.name}'."
            ) from exc
        except OSError as exc:
            raise ContextAccessDeniedError(
                f"Error de acceso al inspeccionar '{path_obj.name}' ({type(exc).__name__})."
            ) from exc

        if self.is_stat_reparse_or_symlink(st, path_obj):
            raise ContextAccessDeniedError(
                f"Ruta rechazada: se detectó un enlace simbólico o junction NTFS en '{path_obj.name}'."
            )

    def validate_read_path(
        self,
        raw_path: str | Path,
        *,
        must_exist: bool = False,
    ) -> tuple[Path, Path]:
        """Valida defensivamente que una ruta sea segura y pertenezca a `self.authorized_roots`.

        Retorna `(resolved_path, matching_authorized_root)` o lanza `ContextAccessDeniedError`.
        """
        if raw_path is None:
            raise ContextAccessDeniedError("Ruta rechazada: no se especificó ninguna ruta.")

        raw_str = str(raw_path).strip()
        if not raw_str or "\x00" in raw_str:
            raise ContextAccessDeniedError("Ruta rechazada: está vacía o contiene bytes nulos.")

        if "%" in raw_str:
            raise ContextAccessDeniedError(
                "Ruta rechazada: no se permiten variables de entorno sin expandir ('%...%')."
            )

        if raw_str.startswith(("\\\\", "//")):
            raise ContextAccessDeniedError(
                "Ruta rechazada: no se permiten rutas de red UNC ni dispositivos directos."
            )

        # F1: Rechazar Alternate Data Streams (ADS) o cualquier ':' fuera del prefijo de unidad ('C:\')
        rest_after_drive = raw_str[2:] if re.match(r"^[A-Za-z]:[\\/]", raw_str) else raw_str
        if ":" in rest_after_drive:
            raise ContextAccessDeniedError(
                "Ruta rechazada: no se permiten flujos de datos alternativos NTFS (ADS) ni ':' en segmentos."
            )

        candidate = Path(raw_str)
        if ".." in candidate.parts:
            raise ContextAccessDeniedError(
                "Ruta rechazada: se detectaron segmentos de path traversal ('..')."
            )

        if not candidate.is_absolute():
            raise ContextAccessDeniedError(
                "Ruta rechazada: el proveedor de archivos exige rutas absolutas explícitas."
            )

        for idx, part in enumerate(candidate.parts):
            if idx > 0 and ":" in part:
                raise ContextAccessDeniedError(
                    f"Ruta rechazada: se detectó sintaxis de Alternate Data Stream (ADS) en '{part}'."
                )
            stem_upper = part.split(".")[0].strip().upper()
            if stem_upper in WINDOWS_RESERVED_NAMES:
                raise ContextAccessDeniedError(
                    f"Ruta rechazada: '{part}' es un nombre de dispositivo reservado de Windows."
                )
            if idx > 0:
                clean_seg = part.strip().rstrip(". ").lower()
                if clean_seg in FORBIDDEN_CONTEXT_DIR_NAMES:
                    raise ContextAccessDeniedError(
                        f"Acceso denegado por privacidad: el directorio '{part}' está excluido en N7."
                    )
                if is_sensitive_context_filename(part):
                    raise ContextAccessDeniedError(
                        f"Acceso denegado por privacidad: '{part}' es un archivo sensible protegido."
                    )

        # Inspeccionar enlaces simbólicos, junctions NTFS (incluyendo rotos) y permisos en la ruta y ancestros
        current_check: Path = candidate
        visited: set[str] = set()
        while True:
            key = str(current_check)
            if key in visited or current_check.parent == current_check:
                break
            visited.add(key)
            self._assert_path_node_safe(current_check)
            if current_check in self.authorized_roots:
                break
            current_check = current_check.parent

        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"La ruta solicitada no existe: '{candidate.name}'.") from exc
        except PermissionError as exc:
            raise ContextAccessDeniedError(
                f"Acceso denegado por permisos al resolver la ruta '{candidate.name}'."
            ) from exc
        except OSError as exc:
            raise ContextAccessDeniedError(
                f"No fue posible resolver de forma segura la ruta solicitada ({type(exc).__name__})."
            ) from exc

        resolved_lower = str(resolved).lower().rstrip("\\/")
        if any(
            resolved_lower == forbidden or resolved_lower.startswith(forbidden + "\\")
            for forbidden in settings.FORBIDDEN_SYSTEM_PREFIXES
        ):
            raise ContextAccessDeniedError(
                "Acceso denegado: está prohibido inspeccionar directorios críticos del sistema Windows."
            )

        if is_sensitive_context_filename(resolved.name):
            raise ContextAccessDeniedError(
                f"Acceso denegado por privacidad: el archivo '{resolved.name}' está protegido."
            )

        matching_root: Optional[Path] = None
        for root in self.authorized_roots:
            try:
                if resolved.is_relative_to(root):
                    matching_root = root
                    break
            except ValueError:
                continue

        if matching_root is None:
            raise ContextAccessDeniedError(
                "Acceso denegado: la ruta se encuentra fuera de las raíces autorizadas de J.A.R.V.I.S."
            )

        # F3: Verificar exclusión de subárboles y directorios sensibles sobre la ruta canónica resuelta
        if self.is_forbidden_context_subpath(resolved, matching_root):
            raise ContextAccessDeniedError(
                f"Acceso denegado por privacidad: '{resolved.name}' pertenece a una ubicación sensible excluida en N7."
            )

        # Segunda verificación de la cadena canónica entre matching_root y resolved
        chain_cursor: Path = resolved
        while chain_cursor != matching_root and chain_cursor.parent != chain_cursor:
            self._assert_path_node_safe(chain_cursor)
            chain_cursor = chain_cursor.parent

        if must_exist:
            try:
                st_final = os.lstat(resolved)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"La ruta solicitada no existe: '{resolved.name}'.") from exc
            except PermissionError as exc:
                raise ContextAccessDeniedError(
                    f"Acceso denegado por permisos en '{resolved.name}'."
                ) from exc
            except OSError as exc:
                raise ContextAccessDeniedError(
                    f"Error de sistema de archivos al verificar '{resolved.name}' ({type(exc).__name__})."
                ) from exc
            if self.is_stat_reparse_or_symlink(st_final, resolved):
                raise ContextAccessDeniedError(
                    f"Ruta rechazada: se detectó un enlace simbólico o junction NTFS en '{resolved.name}'."
                )

        return resolved, matching_root
