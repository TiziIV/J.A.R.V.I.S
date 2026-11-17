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
        "prompts_history.txt",
        "memory_store.json",
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
        ".git-credentials",
        ".dockerconfigjson",
        "file_editor_audit.jsonl",
        "jarvis_system.log",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "authorized_keys",
        "known_hosts",
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
    ".kdbx",
    ".jks",
    ".keystore",
    ".ovpn",
    ".gpg",
    ".asc",
    ".der",
    ".cer",
    ".crt",
    ".log",
)

# Sufijos de respaldo/temporales que no deben permitir evadir la detección de archivos sensibles.
_BACKUP_OR_TEMP_SUFFIXES: tuple[str, ...] = (
    ".bak",
    ".backup",
    ".old",
    ".orig",
    ".tmp",
    ".swp",
    ".save",
    ".copy",
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
        ".password-store",
        ".docker",
        "credentials",
        "secrets",
        "keyrings",
        "wallets",
        "user data",
    }
)

# Subárboles relativos o de perfil de usuario bloqueados específicamente en N7 (F3)
# sin alterar las raíces autorizadas compartidas con N5 (`FileEditorSkill`).
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
    "google/chrome/user data",
    "microsoft/edge/user data",
    "bravesoftware/brave-browser/user data",
    "vivaldi/user data",
    "opera software/opera stable",
    "mozilla/firefox/profiles",
    "1password",
    "bitwarden",
    "keepass",
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

# Caracteres de control ASCII, controles bidireccionales Unicode y bloque Tags.
_CONTROL_AND_BIDI_RE: re.Pattern[str] = re.compile(
    r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]+"
)

# Patrones de suplantación de rol, delimitadores o inyección de instrucciones en nombres del sistema (F4).
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Instrucciones de anulación o evasión en inglés y español (con variantes y sinónimos)
    re.compile(
        r"(?i)\b(?:ignore|ignora|ignorar|ignoren|disregard|forget|olvida|olvidar|omite|omitir|"
        r"descarta|descartar|desobedece|desobedecer|anula|anular|bypass|override|salta|saltear|"
        r"reemplaza|sobrescribe)\s+"
        r"(?:all\s+|any\s+|todas\s+las\s+|todos\s+los\s+|las\s+|los\s+|cualquier\s+)?"
        r"(?:previous\s+|prior\s+|above\s+|system\s+|safety\s+|security\s+|"
        r"anteriores\s+|previas\s+|previos\s+|del\s+sistema\s+|de\s+seguridad\s+)?"
        r"(?:instructions|instrucciones|rules|reglas|policies|pol[ií]ticas|constraints|"
        r"restricciones|directives|directivas|orders|[oó]rdenes|prompts|guidelines|l[ií]mites)"
    ),
    # Etiquetas o delimitadores XML/HTML/corchetes de cierre o apertura de evidencia/sistema
    re.compile(
        r"(?i)<\s*/?\s*(?:evidencia[a-z0-9_]*|untrusted[a-z0-9_]*|system|assistant|developer|"
        r"user|instruction[s]?|prompt|context[o]?|tool[a-z0-9_]*|function[a-z0-9_]*)\b[^>]*>"
    ),
    re.compile(
        r"(?i)\[\s*/?\s*(?:evidencia[a-z0-9_]*|untrusted[a-z0-9_]*|system|assistant|developer|inst)\b[^\]]*\]"
    ),
    # Marcadores especiales de tokens de control de LLM (<|system|>, <|im_start|>, <<SYS>>, ### System:)
    re.compile(
        r"(?i)(?:<\|[a-z0-9_:\-]+\|>|<<\s*/?\s*sys\s*>>|###\s*(?:system|instruction[s]?|assistant|user|developer|sistema|instrucci[oó]n)\b\s*:?)"
    ),
    # Indicadores de rol directos en inglés y español
    re.compile(r"(?i)\b(?:system|assistant|developer|user|sistema|asistente|desarrollador)\s*:"),
    # Objetos o fragmentos JSON que simulan invocación de herramientas o comandos
    re.compile(
        r"(?i)\{\s*['\"]?(?:action|tool|tool_name|skill_name|command|function|function_call|tool_calls|arguments|skill_params)['\"]?\s*:"
    ),
    re.compile(
        r"(?i)['\"]?(?:tool_name|skill_name|function_call|tool_calls)['\"]?\s*:\s*['\"][a-z0-9_.\-]+['\"]"
    ),
    # Órdenes directas de ejecución de herramientas críticas infiltradas en etiquetas
    re.compile(
        r"(?i)\b(?:ejecuta|ejecutar|invoca|invocar|run|execute|invoke|call)\s+"
        r"(?:la\s+herramienta\s+|la\s+skill\s+|el\s+comando\s+|tool\s+|skill\s+|command\s+)?"
        r"(?:file_editor|system_power|app_launcher|chain_orchestrator|policy_engine|powershell|cmd\.exe)\b"
    ),
)


def _check_single_sensitive_basename(clean_name: str) -> bool:
    """Evalúa si un nombre base normalizado (sin sufijos ADS ni puntos finales) es sensible."""
    if not clean_name:
        return False
    if clean_name in FORBIDDEN_CONTEXT_FILENAMES:
        return True
    if clean_name.startswith(".env.") or clean_name.endswith(".env"):
        if clean_name not in {".env.example", ".env.template", ".env.sample", ".env.dist"}:
            return True
    if any(clean_name.endswith(ext) for ext in FORBIDDEN_CONTEXT_EXTENSIONS):
        return True
    if clean_name.startswith(("client_secret", "service_account", "gcloud_credentials", "firebase-adminsdk")):
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


def is_sensitive_context_filename(filename: str) -> bool:
    """Determina si un nombre de archivo corresponde a un secreto, ADS o almacén privado (F1, F3).

    - Rechaza de forma conservadora cualquier nombre con sintaxis de Alternate Data Stream (`:`).
    - Normaliza espacios y puntos finales de Windows antes de evaluar las listas de exclusión.
    - Detecta copias de respaldo (`.bak`, `.old`, `.tmp`, etc.) de archivos sensibles.
    """
    raw_clean = (filename or "").strip().lower()
    if not raw_clean:
        return False

    # F1: Cualquier presencia de ':' en un nombre de archivo indica un Alternate Data Stream (ADS)
    # o sintaxis no permitida fuera del identificador de unidad; se rechaza conservadoramente.
    if ":" in raw_clean:
        return True

    name_low = raw_clean.rstrip(". ")
    if not name_low:
        return False

    if _check_single_sensitive_basename(name_low):
        return True

    # Evitar evasión mediante sufijos de backup/temporales (ej. credentials.json.bak, .env.old)
    stripped = name_low
    for suffix in _BACKUP_OR_TEMP_SUFFIXES:
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            stripped = stripped[: -len(suffix)].rstrip(". ")
            if _check_single_sensitive_basename(stripped):
                return True

    return False


def sanitize_untrusted_label(raw_text: Any, *, max_len: int = 160) -> str:
    """Sanitiza un texto proveniente del sistema operativo (nombre de proceso, ventana o archivo).

    1. Redacta cualquier posible clave API, token o contraseña mediante `redact_secrets`.
    2. Elimina saltos de línea, caracteres de control y marcadores invisibles Unicode.
    3. Neutraliza patrones de inyección de instrucciones o suplantación de rol.
    4. Acota la longitud máxima a `max_len` caracteres.
    """
    if raw_text is None:
        return ""
    text = redact_secrets(str(raw_text))
    text = _CONTROL_AND_BIDI_RE.sub(" ", text)
    for pattern in _INJECTION_PATTERNS:
        text = pattern.sub("[DATO_SISTEMA_FILTRADO]", text)
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
