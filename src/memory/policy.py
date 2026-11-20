"""
Políticas de seguridad, sanitización, validación de rutas y elegibilidad de memoria (N8.1).

Justificación técnica junior:
La memoria a largo plazo persiste entre sesiones. Si permitiéramos guardar secretos,
transcripciones completas, rutas UNC remotas o alucinaciones no verificadas (`INFERENCE`),
contaminaríamos futuras conversaciones o expondríamos credenciales en disco. Este módulo
filtra y normaliza todo ANTES de tocar SQLite.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import unicodedata
from typing import Iterable

from config import settings
from src.brain.exceptions import is_sensitive_key, redact_secrets
from src.memory.models import (
    ExecutionVerificationEvidence,
    MemoryCategory,
    MemoryCreateInput,
    MemoryPolicyError,
    MemoryPrivateModeError,
    MemoryProvenance,
    MemoryScopeType,
    MemorySource,
    MemoryValidationError,
    canonical_json,
)


# Nombres reservados heredados de MS-DOS/Windows que fallan o bloquean operaciones de archivo.
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

FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

# Combinación permitida (fuente -> categorías). N8 no acepta INFERENCE ni preferencias inventadas por ejecución.
SOURCE_ALLOWED_CATEGORIES: dict[MemorySource, frozenset[MemoryCategory]] = {
    MemorySource.USER_EXPLICIT: frozenset(
        {
            MemoryCategory.FACT,
            MemoryCategory.PREFERENCE,
            MemoryCategory.DECISION,
            MemoryCategory.EXPERIENCE,
        }
    ),
    MemorySource.DECISION_CONFIRMED: frozenset(
        {
            MemoryCategory.DECISION,
            MemoryCategory.PREFERENCE,
            MemoryCategory.FACT,
        }
    ),
    MemorySource.EXECUTION_VERIFIED: frozenset(
        {
            MemoryCategory.EXPERIENCE,
            MemoryCategory.FACT,
        }
    ),
}

# Detectores complementarios a src.brain.exceptions.redact_secrets para no persistir secretos.
_PEM_PRIVATE_KEY_RE: re.Pattern[str] = re.compile(
    r"-----BEGIN\s+(?:[A-Z0-9 ]+\s+)?PRIVATE KEY-----",
    re.IGNORECASE,
)
_ENV_ASSIGNMENT_SECRET_RE: re.Pattern[str] = re.compile(
    r"(?im)^\s*(?:[A-Z][A-Z0-9_]*?(?:API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|CREDENTIAL))\s*=\s*['\"]?[^\s'\"]{6,}"
)
_PROMPT_CONTROL_DELIM_RE: re.Pattern[str] = re.compile(
    r"(?i)(?:<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>|<<SYS>>|<</SYS>>|<script\b|</script>)"
)
_ANSI_ESCAPE_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TRANSCRIPT_TURN_RE: re.Pattern[str] = re.compile(
    r"(?im)^\s*(?:\[?\s*(?:usuario|user|asistente|assistant|jarvis|j\.a\.r\.v\.i\.s\.?|system|sistema)\s*\]?\s*:)"
)
_UNEXPANDED_ENV_RE: re.Pattern[str] = re.compile(r"%[^%/\\\s]{1,64}%|\$\{[^}\s]{1,64}\}")
_WHITESPACE_RUN_RE: re.Pattern[str] = re.compile(r"[ \t\f\v]+")
_MULTI_NEWLINE_RE: re.Pattern[str] = re.compile(r"\n{3,}")
_SEARCH_SEP_RE: re.Pattern[str] = re.compile(
    r"[.\-_]{2,}|(?<![0-9A-Za-z_])[.\-_]+|[.\-_]+(?![0-9A-Za-z_])|[^\w.\-]+"
)


def is_test_environment() -> bool:
    """Indica si el proceso corre bajo aislamiento de pruebas (`JARVIS_ENV=test|testing`)."""
    return (os.getenv("JARVIS_ENV") or "").strip().lower() in {"test", "testing"}


def resolve_test_sandbox_root() -> Path:
    """
    Devuelve la raíz segura dentro de `temp/` para bases SQLite de prueba.

    Justificación técnica junior:
    En Windows del entorno actual `%TEMP%` puede tener restricciones de ACL, mientras que
    `data/` contiene la memoria real del usuario. Centralizar las pruebas en
    `temp/jarvis_test_isolated` protege los datos reales y garantiza permisos consistentes.
    """
    repo_temp = (settings.BASE_DIR / "temp").resolve()
    configured = (os.getenv("JARVIS_TEST_TEMP_ROOT") or "").strip()
    if configured:
        candidate = Path(configured).resolve()
        try:
            candidate.relative_to(repo_temp)
            return candidate
        except ValueError:
            pass
    return (repo_temp / "jarvis_test_isolated").resolve()


def _is_reparse_point_windows(path: Path) -> bool:
    """Detecta symlinks, junctions o reparse points NTFS mediante Win32 API sin resolver el destino."""
    if os.name != "nt":
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF:
            return False
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return False


def _is_path_symlink_or_reparse(path: Path) -> bool:
    """Comprueba el nodo destino y cada padre existente contra symlinks o junctions NTFS."""
    current = Path(os.path.abspath(str(path)))
    for node in (current, *current.parents):
        try:
            if node.exists() or node.is_symlink():
                if node.is_symlink() or _is_reparse_point_windows(node):
                    return True
        except OSError:
            return True
    return False


def _validate_local_safe_path(
    raw_path: Path | str,
    *,
    allowed_suffixes: frozenset[str] | None = None,
    allow_production_in_tests: bool = False,
    label: str = "almacenamiento N8",
) -> Path:
    """
    Valida que una ruta de archivo o directorio sea estrictamente local, libre de UNC/SMB,
    traversal, nombres reservados de Windows, reparse points NTFS y respete el sandbox de pruebas.
    """
    if not isinstance(raw_path, (str, Path)):
        raise MemoryValidationError(f"La ruta de {label} debe ser str o Path.")

    raw_text = str(raw_path).strip()
    if not raw_text:
        raise MemoryValidationError(f"La ruta de {label} no puede estar vacía.")
    if "\x00" in raw_text:
        raise MemoryPolicyError("Ruta rechazada por contener byte NUL.")

    lowered_raw = raw_text.lower()
    if (
        raw_text.startswith("\\\\")
        or raw_text.startswith("//")
        or lowered_raw.startswith("\\\\?\\unc\\")
        or lowered_raw.startswith("\\\\.\\")
    ):
        raise MemoryPolicyError(f"Se prohíben rutas UNC/SMB o de dispositivo para {label}.")

    if _UNEXPANDED_ENV_RE.search(raw_text):
        raise MemoryPolicyError(f"Se prohíben variables de entorno sin expandir en {label}.")

    win_parts = PureWindowsPath(raw_text).parts
    if ".." in win_parts:
        raise MemoryPolicyError(f"Se prohíbe traversal '..' en rutas de {label}.")

    for part in win_parts:
        stem = Path(part).stem.upper()
        name_upper = part.rstrip(".").upper()
        if stem in WINDOWS_RESERVED_NAMES or name_upper in WINDOWS_RESERVED_NAMES:
            raise MemoryPolicyError(f"Nombre reservado Windows no permitido en ruta: {part!r}")

    abs_candidate = Path(os.path.abspath(raw_text))
    if _is_path_symlink_or_reparse(abs_candidate):
        raise MemoryPolicyError(f"Se prohíben symlinks o NTFS reparse points en {label}.")

    resolved = abs_candidate.resolve()
    if allowed_suffixes is not None and resolved.suffix.lower() not in allowed_suffixes:
        exts = ", ".join(sorted(allowed_suffixes))
        raise MemoryValidationError(f"La ruta de {label} debe usar extensión válida ({exts}).")

    resolved_str = os.path.normcase(str(resolved))
    for prefix in settings.FORBIDDEN_SYSTEM_PREFIXES:
        norm_prefix = os.path.normcase(prefix.rstrip("\\/"))
        if resolved_str == norm_prefix or resolved_str.startswith(norm_prefix + os.sep):
            raise MemoryPolicyError(f"No se permite ubicar {label} en directorios protegidos del SO.")

    if is_test_environment() and not allow_production_in_tests:
        prod_data_dir = (settings.BASE_DIR / "data").resolve()
        try:
            resolved.relative_to(prod_data_dir)
        except ValueError:
            pass
        else:
            raise MemoryPolicyError(
                "Acceso bloqueado a `data/` real mientras JARVIS_ENV=test está activo."
            )

        sandbox_root = resolve_test_sandbox_root()
        repo_temp = (settings.BASE_DIR / "temp").resolve()
        in_sandbox = False
        for allowed_root in (sandbox_root, repo_temp):
            try:
                resolved.relative_to(allowed_root)
                in_sandbox = True
                break
            except ValueError:
                continue
        if not in_sandbox:
            raise MemoryPolicyError(
                f"En JARVIS_ENV=test la ruta de {label} debe residir dentro de {sandbox_root}."
            )

    return resolved


def validate_safe_local_db_path(
    db_path: Path | str,
    *,
    allow_production_in_tests: bool = False,
) -> Path:
    """Valida que la ruta SQLite sea estrictamente local, segura y aislada en pruebas."""
    return _validate_local_safe_path(
        db_path,
        allowed_suffixes=frozenset({".db", ".sqlite", ".sqlite3"}),
        allow_production_in_tests=allow_production_in_tests,
        label="base SQLite N8",
    )


def validate_safe_backup_dir(
    backup_dir: Path | str,
    *,
    allow_production_in_tests: bool = False,
) -> Path:
    """Valida el directorio local de snapshots de respaldo sin permitir escapes ni `data/` en tests."""
    return _validate_local_safe_path(
        backup_dir,
        allowed_suffixes=None,
        allow_production_in_tests=allow_production_in_tests,
        label="backups N8",
    )


def validate_safe_read_json_path(
    json_path: Path | str,
    *,
    allow_production_in_tests: bool = False,
) -> Path:
    """Valida una ruta JSON local de lectura para importación manual de preferencias."""
    return _validate_local_safe_path(
        json_path,
        allowed_suffixes=frozenset({".json"}),
        allow_production_in_tests=allow_production_in_tests,
        label="importación JSON N8",
    )


def _is_variation_selector(codepoint: int) -> bool:
    return (0xFE00 <= codepoint <= 0xFE0F) or (0xE0100 <= codepoint <= 0xE01EF)


def _strip_dangerous_unicode(text: str) -> str:
    """
    Normaliza con NFKC y elimina caracteres invisibles de formato (Cf), U+034F y selectores
    que podrían ocultar texto malicioso o confundir comparaciones deterministas.
    """
    normalized = unicodedata.normalize("NFKC", text)
    cleaned_chars: list[str] = []
    for ch in normalized:
        cp = ord(ch)
        if cp == 0:
            raise MemoryValidationError("El texto contiene byte NUL no permitido.")
        if ch == "\r":
            cleaned_chars.append("\n")
            continue
        if ch == "\t":
            cleaned_chars.append(" ")
            continue
        cat = unicodedata.category(ch)
        if cat == "Cs":
            raise MemoryValidationError("El texto contiene surrogates Unicode inválidos.")
        if cat == "Cf" or cp == 0x034F or _is_variation_selector(cp):
            continue
        if cat == "Cc" and ch != "\n":
            raise MemoryValidationError("El texto contiene caracteres de control no permitidos.")
        cleaned_chars.append(ch)
    return "".join(cleaned_chars)


def reject_malformed_or_untrusted_payload(text: str, *, field_name: str = "content") -> None:
    """
    Rechaza payloads claramente malformados, secuencias ANSI, delimitadores de prompt
    o transcripciones conversacionales completas que no deben almacenarse en N8.
    """
    if _ANSI_ESCAPE_RE.search(text):
        raise MemoryValidationError(f"El campo '{field_name}' contiene secuencias de escape ANSI no válidas.")
    if _PROMPT_CONTROL_DELIM_RE.search(text):
        raise MemoryPolicyError(
            f"El campo '{field_name}' contiene delimitadores de control/script no permitidos."
        )
    turn_matches = _TRANSCRIPT_TURN_RE.findall(text)
    if len(turn_matches) >= 4:
        raise MemoryPolicyError(
            f"El campo '{field_name}' parece una transcripción conversacional completa; N8 solo persiste síntesis/hechos."
        )


def assert_no_secrets(text: str, *, field_name: str = "texto") -> None:
    """
    Detecta secretos o credenciales evidentes reutilizando `redact_secrets` y reglas propias,
    rechazando la ingesta sin reflejar nunca el secreto en mensajes ni excepciones.
    """
    if not text:
        return
    if redact_secrets(text) != text:
        raise MemoryPolicyError(
            f"Contenido en '{field_name}' rechazado por coincidir con patrón de credencial o API key."
        )
    if _PEM_PRIVATE_KEY_RE.search(text):
        raise MemoryPolicyError(
            f"Contenido en '{field_name}' rechazado por incluir encabezado de clave privada PEM."
        )
    if _ENV_ASSIGNMENT_SECRET_RE.search(text):
        raise MemoryPolicyError(
            f"Contenido en '{field_name}' rechazado por incluir asignación tipo variable de entorno sensible."
        )


def sanitize_memory_text(
    text: str,
    *,
    max_chars: int,
    allow_newlines: bool = True,
    field_name: str = "content",
    check_secrets: bool = True,
) -> str:
    """
    Sanitizador específico de N8 para textos completos de recuerdos.

    Justificación técnica junior:
    A diferencia de `sanitize_untrusted_label()` de N7 (que recorta a 160 caracteres y altera
    frases cortas), `sanitize_memory_text()` preserva la prosa técnica o de preferencia del usuario
    dentro de los presupuestos N8 mientras elimina Unicode invisible/bidi, controla saltos
    de línea, bloquea transcripciones/delimitadores y rechaza secretos evidentes.
    """
    if not isinstance(text, str):
        raise MemoryValidationError(f"El campo '{field_name}' debe ser una cadena de texto.")

    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MemoryValidationError(f"El campo '{field_name}' no es UTF-8 válido.") from exc

    reject_malformed_or_untrusted_payload(text, field_name=field_name)
    stripped = _strip_dangerous_unicode(text)

    if allow_newlines:
        lines = [_WHITESPACE_RUN_RE.sub(" ", line).strip() for line in stripped.split("\n")]
        cleaned = _MULTI_NEWLINE_RE.sub("\n\n", "\n".join(lines)).strip()
    else:
        cleaned = _WHITESPACE_RUN_RE.sub(" ", stripped.replace("\n", " ")).strip()

    if not cleaned:
        raise MemoryValidationError(f"El campo '{field_name}' quedó vacío tras sanitizar.")

    if len(cleaned) > max_chars:
        raise MemoryValidationError(
            f"El campo '{field_name}' excede el límite permitido de {max_chars} caracteres ({len(cleaned)})."
        )

    if check_secrets:
        assert_no_secrets(cleaned, field_name=field_name)

    return cleaned


def normalize_text_for_search(text: str) -> str:
    """
    Genera forma canónica sin acentos y en minúsculas para deduplicación y respaldo SQL.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    no_marks = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    lowered = no_marks.lower().strip()
    return _SEARCH_SEP_RE.sub(" ", lowered).strip()


def normalize_scope(
    scope_type: MemoryScopeType,
    scope_key: str,
    *,
    max_scope_key_chars: int = 120,
) -> tuple[MemoryScopeType, str]:
    """Normaliza y valida el ámbito para impedir valores vacíos, rutas UNC o secret leakage."""
    if not isinstance(scope_type, MemoryScopeType):
        scope_type = MemoryScopeType(scope_type)

    if scope_type == MemoryScopeType.GLOBAL:
        return MemoryScopeType.GLOBAL, "global"

    if not isinstance(scope_key, str) or not scope_key.strip():
        raise MemoryValidationError(f"El ámbito {scope_type.value} requiere un scope_key no vacío.")

    raw_scope = scope_key.strip()
    if raw_scope.startswith("\\\\") or raw_scope.startswith("//") or ".." in Path(raw_scope).parts:
        raise MemoryPolicyError("scope_key no puede contener rutas UNC ni traversal '..'.")
    if _UNEXPANDED_ENV_RE.search(raw_scope):
        raise MemoryPolicyError("scope_key no puede contener variables de entorno sin expandir.")

    cleaned = sanitize_memory_text(
        raw_scope,
        max_chars=max_scope_key_chars,
        allow_newlines=False,
        field_name="scope_key",
        check_secrets=True,
    ).lower()
    if cleaned == "global":
        raise MemoryValidationError(
            f"El scope_key 'global' está reservado para MemoryScopeType.GLOBAL, no para {scope_type.value}."
        )
    return scope_type, cleaned


def compute_content_fingerprint(
    *,
    category: MemoryCategory,
    scope_type: MemoryScopeType,
    scope_key: str,
    normalized_text: str,
) -> str:
    """Calcula huella SHA-256 determinista usada para deduplicar recuerdos activos equivalentes."""
    payload = {
        "category": category.value,
        "scope_type": scope_type.value,
        "scope_key": scope_key,
        "normalized_text": normalized_text,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreparedMemoryCreate:
    """Resultado validado, sanitizado y listo para insertarse transaccionalmente en SQLite."""

    category: MemoryCategory
    source: MemorySource
    scope_type: MemoryScopeType
    scope_key: str
    title: str
    content: str
    summary: str
    normalized_text: str
    content_fingerprint: str
    confidence: float
    importance: int
    tags: tuple[str, ...]
    metadata: tuple[tuple[str, str], ...]
    provenance: MemoryProvenance
    verification_evidence: ExecutionVerificationEvidence | None
    expires_at: str | None


class MemoryPolicy:
    """
    Motor de políticas N8.1: controla elegibilidad, longitudes, secretos y modo privado.

    Justificación técnica junior:
    Esta clase nunca aprueba acciones de sistema ni modifica reglas de N5; únicamente decide
    qué recuerdos pueden guardarse en `experiences.db` y en qué forma limpia y acotada.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        allow_inference_ingest: bool | None = None,
        max_active_memories: int | None = None,
        max_title_chars: int | None = None,
        max_content_chars: int | None = None,
        max_summary_chars: int | None = None,
        max_revisions_per_memory: int | None = None,
        max_tags_per_memory: int = 12,
        max_metadata_entries: int = 16,
    ) -> None:
        self.enabled: bool = settings.N8_ENABLED if enabled is None else bool(enabled)
        self.allow_inference_ingest: bool = (
            settings.N8_ALLOW_INFERENCE_INGEST
            if allow_inference_ingest is None
            else bool(allow_inference_ingest)
        )
        self.max_active_memories: int = (
            settings.N8_MAX_ACTIVE_MEMORIES if max_active_memories is None else int(max_active_memories)
        )
        self.max_title_chars: int = (
            settings.N8_MAX_TITLE_CHARS if max_title_chars is None else int(max_title_chars)
        )
        self.max_content_chars: int = (
            settings.N8_MAX_CONTENT_CHARS if max_content_chars is None else int(max_content_chars)
        )
        self.max_summary_chars: int = (
            settings.N8_MAX_SUMMARY_CHARS if max_summary_chars is None else int(max_summary_chars)
        )
        self.max_revisions_per_memory: int = (
            settings.N8_MAX_REVISIONS_PER_MEMORY
            if max_revisions_per_memory is None
            else int(max_revisions_per_memory)
        )
        self.max_tags_per_memory: int = max(1, int(max_tags_per_memory))
        self.max_metadata_entries: int = max(1, int(max_metadata_entries))

    def assert_enabled(self) -> None:
        """Verifica que N8 esté habilitado antes de aceptar operaciones del almacén."""
        if not self.enabled:
            raise MemoryPolicyError("El subsistema N8 está deshabilitado por configuración (N8_ENABLED=False).")

    def assert_write_allowed(self, *, private_mode: bool) -> None:
        """Bloquea estrictamente cualquier escritura cuando el modo privado está activo."""
        self.assert_enabled()
        if private_mode:
            raise MemoryPrivateModeError(
                "Operación de escritura rechazada: el modo privado está activo."
            )

    def sanitize_tags(self, tags: Iterable[str]) -> tuple[str, ...]:
        """Normaliza, desduplica y valida etiquetas contra secretos o exceso de tamaño."""
        result: list[str] = []
        seen: set[str] = set()
        for raw_tag in tags:
            if not isinstance(raw_tag, str):
                raise MemoryValidationError("Cada tag debe ser una cadena de texto.")
            clean = sanitize_memory_text(
                raw_tag,
                max_chars=48,
                allow_newlines=False,
                field_name="tag",
                check_secrets=True,
            ).lower()
            if clean not in seen:
                seen.add(clean)
                result.append(clean)
        if len(result) > self.max_tags_per_memory:
            raise MemoryValidationError(
                f"Demasiados tags ({len(result)}); el máximo permitido es {self.max_tags_per_memory}."
            )
        return tuple(result)

    def sanitize_metadata(
        self,
        metadata: Iterable[tuple[str, str]],
    ) -> tuple[tuple[str, str], ...]:
        """Valida pares clave/valor seguros rechazando nombres de clave sensibles y secretos."""
        items: list[tuple[str, str]] = []
        seen_keys: set[str] = set()
        for entry in metadata:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise MemoryValidationError("Cada entrada de metadata debe ser una tupla (clave, valor).")
            raw_k, raw_v = entry
            clean_k = sanitize_memory_text(
                str(raw_k),
                max_chars=64,
                allow_newlines=False,
                field_name="metadata.key",
                check_secrets=True,
            ).lower()
            if is_sensitive_key(clean_k):
                raise MemoryPolicyError(
                    f"Clave de metadata rechazada por política de privacidad/secretos: '{clean_k}'."
                )
            clean_v = sanitize_memory_text(
                str(raw_v),
                max_chars=240,
                allow_newlines=False,
                field_name=f"metadata[{clean_k}]",
                check_secrets=True,
            )
            if clean_k not in seen_keys:
                seen_keys.add(clean_k)
                items.append((clean_k, clean_v))
        if len(items) > self.max_metadata_entries:
            raise MemoryValidationError(
                f"Demasiadas entradas en metadata ({len(items)}); máximo {self.max_metadata_entries}."
            )
        items.sort(key=lambda pair: pair[0])
        return tuple(items)

    def validate_execution_evidence(
        self,
        evidence: ExecutionVerificationEvidence,
        *,
        expected_scope_type: MemoryScopeType,
        expected_scope_key: str,
    ) -> ExecutionVerificationEvidence:
        """
        Verifica coherencia de ámbito y ausencia de secretos dentro de la evidencia verificada.
        """
        if not isinstance(evidence, ExecutionVerificationEvidence):
            raise MemoryValidationError("verification_evidence debe ser ExecutionVerificationEvidence.")

        ev_scope_type, ev_scope_key = normalize_scope(evidence.scope_type, evidence.scope_key)
        if ev_scope_type != expected_scope_type or ev_scope_key != expected_scope_key:
            raise MemoryPolicyError(
                "El ámbito de la evidencia verificada no coincide con el ámbito del recuerdo."
            )

        assert_no_secrets(evidence.executor_component, field_name="evidence.executor_component")
        assert_no_secrets(evidence.operation_type, field_name="evidence.operation_type")
        assert_no_secrets(evidence.expected_outcome, field_name="evidence.expected_outcome")
        assert_no_secrets(evidence.verifier_component, field_name="evidence.verifier_component")
        for idx, artifact in enumerate(evidence.verified_artifacts):
            assert_no_secrets(artifact, field_name=f"evidence.verified_artifacts[{idx}]")
            if artifact.startswith("\\\\") or artifact.startswith("//"):
                raise MemoryPolicyError("No se admiten rutas UNC/SMB en verified_artifacts.")

        return evidence

    def prepare_create(
        self,
        payload: MemoryCreateInput,
        *,
        private_mode: bool = False,
    ) -> PreparedMemoryCreate:
        """Ejecuta validación completa de elegibilidad, longitudes, sanitización y huella."""
        self.assert_write_allowed(private_mode=private_mode)

        if not isinstance(payload, MemoryCreateInput):
            raise MemoryValidationError("Se requiere una instancia de MemoryCreateInput.")

        if payload.category == MemoryCategory.INFERENCE and not self.allow_inference_ingest:
            raise MemoryPolicyError(
                "La categoría INFERENCE está desactivada en N8.1; no se permite ingerir inferencias."
            )

        allowed_categories = SOURCE_ALLOWED_CATEGORIES.get(payload.source, frozenset())
        if payload.category not in allowed_categories:
            raise MemoryPolicyError(
                f"El origen {payload.source.value} no puede crear recuerdos de categoría {payload.category.value}."
            )

        scope_type, scope_key = normalize_scope(payload.scope_type, payload.scope_key)

        title = sanitize_memory_text(
            payload.title,
            max_chars=self.max_title_chars,
            allow_newlines=False,
            field_name="title",
        )
        content = sanitize_memory_text(
            payload.content,
            max_chars=self.max_content_chars,
            allow_newlines=True,
            field_name="content",
        )
        if payload.summary is not None and payload.summary.strip():
            summary = sanitize_memory_text(
                payload.summary,
                max_chars=self.max_summary_chars,
                allow_newlines=False,
                field_name="summary",
            )
        else:
            compact = _WHITESPACE_RUN_RE.sub(" ", content.replace("\n", " ")).strip()
            summary = compact[: self.max_summary_chars]
            if len(compact) > self.max_summary_chars:
                summary = summary[: max(1, self.max_summary_chars - 3)].rstrip() + "..."

        tags = self.sanitize_tags(payload.tags)
        raw_meta = self.sanitize_metadata(payload.metadata)
        filtered_meta = [(k, v) for k, v in raw_meta if k != "confirmation_state"]
        if payload.pending_confirmation:
            filtered_meta.append(("confirmation_state", "pending"))
        filtered_meta.sort(key=lambda pair: pair[0])
        metadata = tuple(filtered_meta)

        evidence: ExecutionVerificationEvidence | None = payload.verification_evidence
        if payload.source == MemorySource.EXECUTION_VERIFIED:
            if evidence is None:
                raise MemoryPolicyError(
                    "Los recuerdos con fuente EXECUTION_VERIFIED requieren ExecutionVerificationEvidence."
                )
            evidence = self.validate_execution_evidence(
                evidence,
                expected_scope_type=scope_type,
                expected_scope_key=scope_key,
            )
            if payload.provenance is not None:
                if payload.provenance.source != MemorySource.EXECUTION_VERIFIED:
                    raise MemoryValidationError("Inconsistencia entre provenance.source y payload.source.")
                if (
                    payload.provenance.source_execution_id
                    and payload.provenance.source_execution_id != evidence.source_execution_id
                ):
                    raise MemoryValidationError(
                        "provenance.source_execution_id no coincide con ExecutionVerificationEvidence."
                    )
            provenance = MemoryProvenance(
                source=MemorySource.EXECUTION_VERIFIED,
                source_layer=evidence.source_layer,
                source_goal_id=evidence.source_goal_id,
                source_plan_id=evidence.source_plan_id,
                source_task_id=evidence.source_task_id,
                source_execution_id=evidence.source_execution_id,
                approval_hash=evidence.approval_hash,
                verifier_component=evidence.verifier_component,
                verified_at=evidence.verified_at,
                evidence_hash=evidence.evidence_sha256,
            )
        else:
            if evidence is not None:
                raise MemoryValidationError(
                    "Solo EXECUTION_VERIFIED puede adjuntar ExecutionVerificationEvidence."
                )
            provenance = payload.provenance or MemoryProvenance(
                source=payload.source,
                source_layer="USER" if payload.source == MemorySource.USER_EXPLICIT else "N6",
            )
            if provenance.source != payload.source:
                raise MemoryValidationError("provenance.source debe coincidir con payload.source.")

        normalized_text = normalize_text_for_search(f"{title} {content}")
        if not normalized_text:
            raise MemoryValidationError("El texto normalizado no contiene términos válidos para indexación.")

        fingerprint = compute_content_fingerprint(
            category=payload.category,
            scope_type=scope_type,
            scope_key=scope_key,
            normalized_text=normalized_text,
        )

        return PreparedMemoryCreate(
            category=payload.category,
            source=payload.source,
            scope_type=scope_type,
            scope_key=scope_key,
            title=title,
            content=content,
            summary=summary,
            normalized_text=normalized_text,
            content_fingerprint=fingerprint,
            confidence=payload.confidence,
            importance=payload.importance,
            tags=tags,
            metadata=metadata,
            provenance=provenance,
            verification_evidence=evidence,
            expires_at=payload.expires_at,
        )
