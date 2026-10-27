"""
Habilidad segura de edición y respaldo de archivos de configuración para Windows (`FileEditorSkill`).

Responsabilidades principales:
1. Validar defensivamente todas las rutas en Windows (expansión controlada en raíces configuradas,
   rechazo de path traversal `..`, rutas UNC, `C:\\Windows`, `System32`, dispositivos reservados,
   enlaces simbólicos, junctions y reparse points en el objetivo y sus directorios padre).
2. Descubrir archivos de configuración de juegos y aplicaciones (`discover_config_file`) de forma
   acotada y determinista sin escanear todo el disco y sin elegir arbitrariamente ante ambigüedad.
3. Crear backups verificables byte a byte (`create_backup`) con hash SHA-256, nombres únicos con
   microsegundos (`<archivo>.YYYYMMDD_HHMMSS_ffffff.bak`), protección contra sobrescritura (`xb`)
   y registro de auditoría.
4. Actualizar archivos `.ini`, `.cfg`, `.txt`, `.json` y `.xml` preservando formato:
   - Parser INI conservador por líneas (sin `configparser`): preserva mayúsculas/minúsculas de
     claves y secciones, comentarios (`;` y `#`), líneas vacías, saltos de línea (`CRLF` o `LF`),
     BOM y codificación original, rechazando claves duplicadas o coincidencias ambiguas.
   - JSON tipado y validado.
   - XML validado con bloqueo estricto de `<!DOCTYPE` y `<!ENTITY` para prevenir ataques XXE.
5. Ejecutar escritura atómica en el mismo directorio del objetivo con `flush()` + `os.fsync()`,
   detección de modificaciones concurrentes por SHA-256 y política de hasta 3 reintentos (100 ms base)
   ante bloqueos transitorios de Windows (`PermissionError` / `WinError 32`).
6. Ejecutar restauración segura (`rollback`) con backup previo del estado actual y detección de
   modificaciones posteriores ajenas a la ejecución original.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from typing import Any, Callable, Optional, Sequence
import uuid
import xml.etree.ElementTree as ET

from config.settings import (
    AUDIT_LOG_FILE,
    AUTHORIZED_FILE_ROOTS,
    BACKUP_STORAGE_DIR,
    BASE_DIR,
    FILE_EDITOR_MAX_DISCOVERY_DEPTH,
    FILE_EDITOR_MAX_DISCOVERY_FILES,
    FILE_EDITOR_MAX_FILE_BYTES,
    FILE_EDITOR_MAX_RETRIES,
    FILE_EDITOR_RETRY_BASE_MS,
    FORBIDDEN_SYSTEM_PREFIXES,
)
from skills.base_skill import BaseSkill
from src.brain.exceptions import redact_secrets


# Nombres de dispositivos reservados de Windows que jamás pueden abrirse como archivos regulares.
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

# Atributo nativo de Windows para detectar junctions, mount points y enlaces simbólicos NTFS.
FILE_ATTRIBUTE_REPARSE_POINT: int = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# Expresión regular para encabezados de sección INI: `[Seccion]` con comentario opcional al final.
_INI_SECTION_RE: re.Pattern[str] = re.compile(
    r"^(?P<indent>\s*)\[(?P<section>[^\[\]\r\n]+?)\](?P<trailing>\s*(?:[;#].*)?)$"
)

# Patrón para detectar declaraciones DTD / entidades externas en XML (prevención estricta de XXE).
_XML_XXE_RE: re.Pattern[str] = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", flags=re.IGNORECASE)


def compute_sha256_bytes(data: bytes) -> str:
    """Calcula el resumen criptográfico SHA-256 hexadecimal de una secuencia de bytes."""
    return hashlib.sha256(data).hexdigest()


def compute_sha256_file(file_path: Path | str) -> str:
    """Calcula el resumen criptográfico SHA-256 hexadecimal leyendo los bytes de un archivo."""
    return compute_sha256_bytes(Path(file_path).read_bytes())


def generate_execution_id() -> str:
    """Genera un identificador único y trazable para cada operación o lote de edición."""
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    short_rand = uuid.uuid4().hex[:6]
    return f"exec_{now_str}_{short_rand}"


def _split_line_ending(raw_line: str) -> tuple[str, str]:
    """Separa el cuerpo de una línea de su salto de línea original (`\\r\\n` o `\\n`)."""
    if raw_line.endswith("\r\n"):
        return raw_line[:-2], "\r\n"
    if raw_line.endswith("\n"):
        return raw_line[:-1], "\n"
    if raw_line.endswith("\r"):
        return raw_line[:-1], "\r"
    return raw_line, ""


def _detect_dominant_newline(text: str) -> str:
    """Determina si el texto utiliza predominantemente `CRLF` (`\\r\\n`) o `LF` (`\\n`)."""
    crlf_count = text.count("\r\n")
    lf_only_count = text.count("\n") - crlf_count
    if crlf_count == 0 and lf_only_count > 0:
        return "\n"
    return "\r\n"


def _decode_bytes_strict(data: bytes) -> tuple[str, str, bytes]:
    """
    Decodifica una secuencia de bytes de manera estricta detectando BOM conocidos
    (`UTF-8-SIG`, `UTF-16-LE`, `UTF-16-BE`) sin reemplazar caracteres inválidos en silencio.

    Returns:
        tuple[str, str, bytes]: `(texto_decodificado, nombre_codec, bytes_bom)`
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="strict"), "utf-8", b"\xef\xbb\xbf"
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="strict"), "utf-16-le", b"\xff\xfe"
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="strict"), "utf-16-be", b"\xfe\xff"

    # Si no hay BOM UTF-16 y existen bytes nulos, se trata de un archivo binario o codificación no soportada
    if b"\x00" in data:
        raise UnicodeDecodeError(
            "utf-8",
            data,
            data.find(b"\x00"),
            data.find(b"\x00") + 1,
            "El archivo contiene bytes nulos sin BOM UTF-16 válido; se aborta para evitar corrupción.",
        )

    try:
        return data.decode("utf-8", errors="strict"), "utf-8", b""
    except UnicodeDecodeError:
        # Algunos archivos INI clásicos de Windows usan cp1252; validamos en modo estricto
        return data.decode("cp1252", errors="strict"), "cp1252", b""


def _encode_text_strict(text: str, codec: str, bom: bytes) -> bytes:
    """Codifica texto de forma estricta anteponiendo exactamente el BOM original."""
    encoded = text.encode(codec, errors="strict")
    return bom + encoded


@dataclass(frozen=True)
class BackupRecord:
    """Registro inmutable de una copia de respaldo verificada."""

    execution_id: str
    target_path: str
    backup_path: str
    original_sha256: str
    backup_sha256: str
    size_bytes: int
    created_at: str
    post_edit_sha256: Optional[str] = None


class AuditLogger:
    """
    Registrador de auditoría en formato JSON Lines para operaciones sobre archivos.
    Sanitiza todos los mensajes para no escribir jamás secretos ni credenciales.
    """

    def __init__(self, log_file: Path | str = AUDIT_LOG_FILE) -> None:
        self.log_file: Path = Path(log_file)
        self.entries: list[dict[str, Any]] = []

    def record(self, event_type: str, execution_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Registra un evento estructurado en memoria y, cuando es posible, en disco."""
        safe_payload: dict[str, Any] = {}
        for k, v in payload.items():
            if isinstance(v, str):
                safe_payload[k] = redact_secrets(v)
            elif isinstance(v, (list, tuple)):
                safe_payload[k] = [redact_secrets(str(item)) if isinstance(item, str) else item for item in v]
            else:
                safe_payload[k] = v

        entry = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "execution_id": execution_id,
            **safe_payload,
        }
        self.entries.append(entry)
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return entry


class BackupManager:
    """
    Administra el catálogo de backups verificados e identificadores de ejecución (`execution_id`)
    para permitir auditoría y restauración trazable.
    """

    def __init__(self, audit_logger: Optional[AuditLogger] = None) -> None:
        self.audit_logger: AuditLogger = audit_logger or AuditLogger()
        self._by_execution_id: dict[str, list[BackupRecord]] = {}
        self._by_backup_path: dict[str, BackupRecord] = {}
        self._by_target_path: dict[str, list[BackupRecord]] = {}

    def register_backup(self, record: BackupRecord) -> None:
        """Registra un backup verificado en los índices internos y en el log de auditoría."""
        self._by_execution_id.setdefault(record.execution_id, []).append(record)
        norm_bak = str(Path(record.backup_path).resolve())
        norm_tgt = str(Path(record.target_path).resolve())
        self._by_backup_path[norm_bak] = record
        self._by_target_path.setdefault(norm_tgt, []).append(record)
        self.audit_logger.record(
            "backup_created",
            record.execution_id,
            asdict(record),
        )

    def update_post_edit_hash(self, backup_path: Path | str, final_sha256: str) -> None:
        """Asocia el hash SHA-256 final resultante de una edición al registro del backup."""
        norm_bak = str(Path(backup_path).resolve())
        existing = self._by_backup_path.get(norm_bak)
        if existing is None:
            return
        updated = BackupRecord(
            execution_id=existing.execution_id,
            target_path=existing.target_path,
            backup_path=existing.backup_path,
            original_sha256=existing.original_sha256,
            backup_sha256=existing.backup_sha256,
            size_bytes=existing.size_bytes,
            created_at=existing.created_at,
            post_edit_sha256=final_sha256,
        )
        self._by_backup_path[norm_bak] = updated
        exec_list = self._by_execution_id.get(existing.execution_id, [])
        for idx, item in enumerate(exec_list):
            if str(Path(item.backup_path).resolve()) == norm_bak:
                exec_list[idx] = updated
        tgt_list = self._by_target_path.get(str(Path(existing.target_path).resolve()), [])
        for idx, item in enumerate(tgt_list):
            if str(Path(item.backup_path).resolve()) == norm_bak:
                tgt_list[idx] = updated

    def get_record_by_backup(self, backup_path: Path | str) -> Optional[BackupRecord]:
        """Obtiene el metadato registrado para un archivo `.bak` específico."""
        return self._by_backup_path.get(str(Path(backup_path).resolve()))

    def get_records_by_execution(self, execution_id: str) -> list[BackupRecord]:
        """Devuelve todos los backups asociados a un `execution_id`."""
        return list(self._by_execution_id.get(execution_id, ()))


class FileEditorSkill(BaseSkill):
    """
    Skill de administración segura de archivos de configuración locales en Windows.
    Implementa validación defensiva de rutas, descubrimiento multi-juego, backups SHA-256,
    actualización conservadora INI/JSON/XML/TXT, escritura atómica con reintentos y rollback.
    """

    def __init__(
        self,
        *,
        authorized_roots: Optional[Sequence[str | Path]] = None,
        backup_dir: Optional[Path | str] = None,
        audit_logger: Optional[AuditLogger] = None,
        backup_manager: Optional[BackupManager] = None,
        max_retries: int = FILE_EDITOR_MAX_RETRIES,
        retry_base_ms: int = FILE_EDITOR_RETRY_BASE_MS,
        max_discovery_depth: int = FILE_EDITOR_MAX_DISCOVERY_DEPTH,
        max_discovery_files: int = FILE_EDITOR_MAX_DISCOVERY_FILES,
        max_file_bytes: int = FILE_EDITOR_MAX_FILE_BYTES,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            name="file_editor",
            description=(
                "Administra de forma segura archivos de configuración autorizados en Windows "
                "(descubrimiento acotado, backups SHA-256, edición conservadora INI/JSON/XML/TXT y rollback)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Operación a realizar: 'discover', 'read', 'backup', 'modify' o 'rollback'.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Ruta absoluta del archivo objetivo dentro de una raíz autorizada.",
                    },
                    "game_or_app_name": {
                        "type": "string",
                        "description": "Nombre del juego o aplicación para búsqueda de configuración.",
                    },
                    "target_filename": {
                        "type": "string",
                        "description": "Nombre del archivo esperado (ej. 'fcsetup.ini', 'settings.json').",
                    },
                    "key_values": {
                        "type": "object",
                        "description": "Diccionario de claves y valores a actualizar de forma conservadora.",
                    },
                    "replacement_content": {
                        "type": "string",
                        "description": "Contenido completo para reemplazo explícito (mutuamente excluyente con key_values).",
                    },
                    "backup_path": {
                        "type": "string",
                        "description": "Ruta del archivo .bak para ejecutar rollback.",
                    },
                },
                "required": ["action"],
            },
        )
        raw_roots = authorized_roots if authorized_roots is not None else AUTHORIZED_FILE_ROOTS
        self.authorized_roots: tuple[Path, ...] = self._normalize_authorized_roots(raw_roots)
        self.backup_dir: Optional[Path] = Path(backup_dir).resolve() if backup_dir else None
        self.audit_logger: AuditLogger = audit_logger or AuditLogger()
        self.backup_manager: BackupManager = backup_manager or BackupManager(self.audit_logger)
        self.max_retries: int = max(int(max_retries), 1)
        self.retry_base_ms: int = max(int(retry_base_ms), 10)
        self.max_discovery_depth: int = max(int(max_discovery_depth), 1)
        self.max_discovery_files: int = max(int(max_discovery_files), 10)
        self.max_file_bytes: int = max(int(max_file_bytes), 1024)
        self._sleep_fn: Callable[[float], None] = sleep_fn

        # Estado de diagnóstico de la última operación
        self.last_discovery_result: dict[str, Any] = {}
        self.last_operation_result: dict[str, Any] = {}

    @staticmethod
    def _normalize_authorized_roots(roots: Sequence[str | Path]) -> tuple[Path, ...]:
        """Normaliza y filtra raíces autorizadas descartando rutas del sistema prohibidas."""
        valid: list[Path] = []
        for item in roots:
            raw_str = os.path.expandvars(str(item)).strip()
            if not raw_str or raw_str.startswith("\\\\"):
                continue
            try:
                resolved = Path(raw_str).resolve()
                lower_str = str(resolved).lower().rstrip("\\/")
                if any(
                    lower_str == forbidden or lower_str.startswith(forbidden + "\\")
                    for forbidden in FORBIDDEN_SYSTEM_PREFIXES
                ):
                    continue
                if resolved not in valid:
                    valid.append(resolved)
            except (OSError, ValueError):
                continue
        return tuple(valid)

    @staticmethod
    def _is_reparse_point_or_symlink(path_obj: Path) -> bool:
        """
        Comprueba si una ruta existente es un enlace simbólico, junction NTFS o reparse point
        de Windows utilizando `os.lstat` sin seguir el enlace.
        """
        try:
            if path_obj.is_symlink():
                return True
            st = os.lstat(path_obj)
            if stat.S_ISLNK(st.st_mode):
                return True
            file_attrs = getattr(st, "st_file_attributes", 0)
            if isinstance(file_attrs, int) and (file_attrs & FILE_ATTRIBUTE_REPARSE_POINT):
                return True
        except OSError:
            return False
        return False

    def validate_authorized_path(
        self,
        raw_path: str | Path,
        *,
        must_exist: bool = True,
        require_regular_file: bool = True,
    ) -> Path:
        """
        Valida defensivamente una ruta de archivo en Windows:
        1. Rechaza rutas vacías, bytes nulos, rutas UNC (`\\\\server\\share`) y nombres de dispositivo reservados.
        2. Rechaza rutas relativas y cualquier segmento de traversal (`..`).
        3. Verifica que ni el objetivo ni sus directorios padre sean enlaces simbólicos o junctions.
        4. Bloquea cualquier acceso a `C:\\Windows`, `System32` o zonas privilegiadas.
        5. Verifica que la ruta canónica final permanezca dentro de `self.authorized_roots`.
        6. Si `must_exist=True` y `require_regular_file=True`, verifica que sea un archivo regular dentro del límite de tamaño.
        """
        if raw_path is None:
            raise PermissionError("Ruta inválida: no se especificó ninguna ruta.")

        raw_str = str(raw_path).strip()
        if not raw_str or "\x00" in raw_str:
            raise PermissionError("Ruta inválida: contiene caracteres nulos o está vacía.")

        # Rechazamos variables de entorno sin expandir en rutas de entrada para evitar redirecciones ambiguas
        if "%" in raw_str:
            raise PermissionError("Ruta rechazada: no se permiten variables de entorno sin expandir en rutas de operación.")

        # Rechazamos rutas UNC o de dispositivos de bajo nivel de Windows
        if raw_str.startswith(("\\\\", "//")):
            raise PermissionError("Ruta rechazada: las rutas de red UNC y dispositivos directos no están permitidos.")

        candidate = Path(raw_str)

        # Rechazamos segmentos de path traversal ('..') antes de resolver
        if ".." in candidate.parts:
            raise PermissionError("Ruta rechazada: se detectaron segmentos de path traversal ('..').")

        if not candidate.is_absolute():
            raise PermissionError("Ruta rechazada: se requiere una ruta absoluta explícita.")

        # Verificamos que ningún componente del nombre sea un dispositivo reservado de Windows (CON, NUL, COM1...)
        for part in candidate.parts:
            stem_upper = part.split(".")[0].strip().upper()
            if stem_upper in WINDOWS_RESERVED_NAMES:
                raise PermissionError(f"Ruta rechazada: '{part}' es un nombre de dispositivo reservado de Windows.")

        # Comprobamos enlaces simbólicos / junctions en la ruta y sus ancestros antes de resolver
        current_check: Path = candidate
        visited_ancestors: set[str] = set()
        while True:
            key_str = str(current_check)
            if key_str in visited_ancestors:
                break
            visited_ancestors.add(key_str)
            if current_check.exists() or current_check.is_symlink():
                if self._is_reparse_point_or_symlink(current_check):
                    raise PermissionError(
                        f"Ruta rechazada por seguridad: se detectó un enlace simbólico o punto de reanálisis en '{current_check.name}'."
                    )
            if current_check.parent == current_check:
                break
            current_check = current_check.parent

        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"El archivo solicitado no existe: '{candidate.name}'.") from exc
        except OSError as exc:
            raise PermissionError(f"No se pudo resolver de forma segura la ruta solicitada ({type(exc).__name__}).") from exc

        # Prohibición absoluta sobre directorios del sistema operativo
        resolved_lower = str(resolved).lower().rstrip("\\/")
        if any(
            resolved_lower == forbidden or resolved_lower.startswith(forbidden + "\\")
            for forbidden in FORBIDDEN_SYSTEM_PREFIXES
        ):
            raise PermissionError("Acceso denegado: está estrictamente prohibido modificar archivos del sistema Windows.")

        # Verificamos pertenencia estricta a al menos una raíz autorizada
        matching_root: Optional[Path] = None
        for root in self.authorized_roots:
            try:
                if resolved.is_relative_to(root):
                    matching_root = root
                    break
            except ValueError:
                continue

        if matching_root is None:
            raise PermissionError(
                "Acceso denegado: la ruta solicitada se encuentra fuera de las raíces autorizadas del proyecto."
            )

        # Re-verificamos que ningún directorio entre matching_root y resolved sea symlink/reparse point
        rel_chain: Path = resolved
        while rel_chain != matching_root and rel_chain.parent != rel_chain:
            if (rel_chain.exists() or rel_chain.is_symlink()) and self._is_reparse_point_or_symlink(rel_chain):
                raise PermissionError(
                    f"Ruta rechazada: se detectó redirección o enlace simbólico en '{rel_chain.name}'."
                )
            rel_chain = rel_chain.parent

        if must_exist:
            if not resolved.exists():
                raise FileNotFoundError(f"El archivo solicitado no existe: '{resolved.name}'.")
            st = os.lstat(resolved)
            if require_regular_file:
                if not stat.S_ISREG(st.st_mode):
                    raise PermissionError("El objetivo debe ser un archivo regular, no un directorio ni dispositivo.")
                if st.st_size > self.max_file_bytes:
                    raise PermissionError(
                        f"El archivo supera el tamaño máximo permitido ({self.max_file_bytes} bytes)."
                    )

        return resolved

    def discover_config_files_detailed(
        self,
        game_or_app_name: str,
        target_filename: str,
    ) -> dict[str, Any]:
        """
        Busca de forma acotada y determinista un archivo de configuración dentro de las raíces
        autorizadas. Devuelve las rutas halladas, el motivo de coincidencia y si existe ambigüedad.
        """
        app_clean = (game_or_app_name or "").strip()
        file_clean = (target_filename or "").strip()

        if not file_clean or "/" in file_clean or "\\" in file_clean or ".." in file_clean:
            result = {
                "status": "error",
                "matches": [],
                "ambiguous": False,
                "reason": "Nombre de archivo objetivo inválido para descubrimiento.",
                "files_examined": 0,
            }
            self.last_discovery_result = result
            return result

        target_lower = file_clean.lower()
        app_tokens = [tok for tok in re.split(r"[^a-z0-9]+", app_clean.lower()) if len(tok) >= 2]

        matches: list[tuple[Path, str]] = []
        files_examined: int = 0
        limit_reached: bool = False

        for root in self.authorized_roots:
            if not root.exists() or not root.is_dir():
                continue

            # Recorrido determinista (ordenado alfabéticamente) con límite de profundidad y cantidad de archivos
            queue: list[tuple[Path, int]] = [(root, 0)]
            while queue:
                current_dir, depth = queue.pop(0)
                if depth > self.max_discovery_depth:
                    continue
                if self._is_reparse_point_or_symlink(current_dir):
                    continue

                try:
                    entries = sorted(os.scandir(current_dir), key=lambda e: e.name.lower())
                except OSError:
                    continue

                subdirs: list[Path] = []
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        entry_path = Path(entry.path)
                        if self._is_reparse_point_or_symlink(entry_path):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            subdirs.append(entry_path)
                        elif entry.is_file(follow_symlinks=False):
                            files_examined += 1
                            if files_examined > self.max_discovery_files:
                                limit_reached = True
                                break
                            if entry.name.lower() == target_lower:
                                st = entry.stat(follow_symlinks=False)
                                if st.st_size <= self.max_file_bytes:
                                    full_lower = str(entry_path).lower()
                                    if not app_tokens or any(tok in full_lower for tok in app_tokens):
                                        matched_tokens = [tok for tok in app_tokens if tok in full_lower]
                                        reason = (
                                            f"Coincidencia exacta de '{entry.name}' en ruta asociada a "
                                            f"'{app_clean}' (tokens: {', '.join(matched_tokens)})"
                                            if matched_tokens
                                            else f"Coincidencia exacta de nombre de archivo '{entry.name}' en raíz autorizada"
                                        )
                                        matches.append((entry_path.resolve(), reason))
                    except OSError:
                        continue

                if limit_reached:
                    break

                # Priorizamos subdirectorios cuyo nombre coincida con el juego/app o 'My Games'/'Saved Games'
                def _dir_priority(d: Path) -> tuple[int, str]:
                    d_low = d.name.lower()
                    if app_tokens and any(t in d_low for t in app_tokens):
                        return (0, d_low)
                    if d_low in ("my games", "saved games", "config", "settings"):
                        return (1, d_low)
                    return (2, d_low)

                subdirs.sort(key=_dir_priority)
                for sd in subdirs:
                    queue.append((sd, depth + 1))

            if limit_reached:
                break

        unique_matches: list[tuple[Path, str]] = []
        seen_paths: set[str] = set()
        for m_path, m_reason in matches:
            key = str(m_path)
            if key not in seen_paths:
                seen_paths.add(key)
                unique_matches.append((m_path, m_reason))

        if len(unique_matches) == 1:
            chosen_path, chosen_reason = unique_matches[0]
            result = {
                "status": "found",
                "selected_path": str(chosen_path),
                "matches": [str(chosen_path)],
                "ambiguous": False,
                "reason": chosen_reason,
                "files_examined": files_examined,
            }
        elif len(unique_matches) > 1:
            result = {
                "status": "ambiguous",
                "selected_path": None,
                "matches": [str(p) for p, _ in unique_matches],
                "ambiguous": True,
                "reason": (
                    f"Se encontraron {len(unique_matches)} instalaciones o archivos candidatos para "
                    f"'{file_clean}'. Seleccione explícitamente la ruta deseada."
                ),
                "files_examined": files_examined,
            }
        else:
            result = {
                "status": "not_found",
                "selected_path": None,
                "matches": [],
                "ambiguous": False,
                "reason": f"No se encontró '{file_clean}' para '{app_clean}' dentro de las raíces autorizadas.",
                "files_examined": files_examined,
            }

        self.last_discovery_result = result
        return result

    def discover_config_file(
        self,
        game_or_app_name: str,
        target_filename: str,
    ) -> Optional[Path]:
        """
        Busca un archivo de configuración para `game_or_app_name` dentro de las raíces autorizadas.
        Devuelve la ruta `Path` solo cuando existe una coincidencia inequívoca; si no existe o
        hay múltiples coincidencias ambiguas, devuelve `None`.
        """
        detailed = self.discover_config_files_detailed(game_or_app_name, target_filename)
        selected = detailed.get("selected_path")
        return Path(selected) if selected else None

    def create_backup(
        self,
        file_path: Path | str,
        *,
        execution_id: Optional[str] = None,
    ) -> Path:
        """
        Crea una copia de seguridad verificada byte a byte antes de cualquier modificación:
        1. Valida la ruta y confirma que es un archivo regular autorizado.
        2. Lee los bytes originales y calcula su SHA-256.
        3. Genera un nombre único `<archivo>.<YYYYMMDD_HHMMSS_ffffff>.bak` sin sobrescribir backups previos (`xb`).
        4. Escribe los bytes exactos, ejecuta `flush()` + `os.fsync()`.
        5. Re-lee el backup y el archivo original para confirmar que ambos hashes SHA-256 coinciden.
        """
        exec_id = execution_id or generate_execution_id()
        validated_target = self.validate_authorized_path(
            file_path,
            must_exist=True,
            require_regular_file=True,
        )

        original_bytes = validated_target.read_bytes()
        original_sha256 = compute_sha256_bytes(original_bytes)

        target_dir = self.backup_dir if self.backup_dir is not None else validated_target.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        # Validamos que el directorio donde se aloja el backup también esté autorizado
        self.validate_authorized_path(target_dir, must_exist=True, require_regular_file=False)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        base_bak_name = f"{validated_target.name}.{timestamp}.bak"
        backup_candidate = target_dir / base_bak_name

        # Protección estricta contra sobrescritura: abrimos con O_CREAT | O_EXCL ('xb').
        # Si ya existe un archivo con el mismo nombre exacto, agregamos sufijo incremental único.
        attempt_suffix = 0
        while True:
            if attempt_suffix > 0:
                backup_candidate = target_dir / f"{validated_target.name}.{timestamp}_{attempt_suffix}.bak"
            try:
                with open(backup_candidate, "xb") as bf:
                    bf.write(original_bytes)
                    bf.flush()
                    os.fsync(bf.fileno())
                break
            except FileExistsError:
                attempt_suffix += 1
                if attempt_suffix > 100:
                    raise RuntimeError("No fue posible generar un nombre de backup único sin colisión.") from None

        # Verificación criptográfica del backup recién escrito
        backup_bytes = backup_candidate.read_bytes()
        backup_sha256 = compute_sha256_bytes(backup_bytes)
        if backup_sha256 != original_sha256:
            try:
                backup_candidate.unlink()
            except OSError:
                pass
            raise RuntimeError("Fallo de integridad: el hash SHA-256 del backup no coincide con el original.")

        # Verificación anti-TOCTOU: comprobamos que el archivo objetivo no cambió mientras se copiaba
        recheck_bytes = validated_target.read_bytes()
        if compute_sha256_bytes(recheck_bytes) != original_sha256:
            raise RuntimeError(
                "Conflicto concurrente: el archivo objetivo fue modificado mientras se generaba el backup."
            )

        record = BackupRecord(
            execution_id=exec_id,
            target_path=str(validated_target),
            backup_path=str(backup_candidate),
            original_sha256=original_sha256,
            backup_sha256=backup_sha256,
            size_bytes=len(original_bytes),
            created_at=datetime.now().isoformat(),
        )
        self.backup_manager.register_backup(record)
        return backup_candidate

    # ------------------------------------------------------------------
    # Actualizadores conservadores por formato (INI, JSON, XML, TXT/CFG)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_key_spec(
        raw_key: str,
        raw_val: Any,
        default_section: Optional[str],
    ) -> tuple[Optional[str], str, str]:
        """
        Normaliza una entrada de `key_values` soportando:
        - `{"WIDTH": 1920}` (con `section` opcional o búsqueda inequívoca)
        - `{"Display.WIDTH": 1920}` o `{"[Display]WIDTH": 1920}`
        - `{"WIDTH": {"section": "Display", "value": 1920}}`
        """
        target_sec = default_section
        key_name = str(raw_key)

        if isinstance(raw_val, dict) and "value" in raw_val:
            if "section" in raw_val and raw_val["section"] is not None:
                target_sec = str(raw_val["section"]).strip()
            val_str = str(raw_val["value"])
        else:
            val_str = str(raw_val)

        if key_name.startswith("[") and "]" in key_name:
            sec_part, rem_key = key_name[1:].split("]", 1)
            target_sec = sec_part.strip()
            key_name = rem_key.strip()
        elif "." in key_name and target_sec is None:
            sec_part, rem_key = key_name.split(".", 1)
            if sec_part.strip() and rem_key.strip():
                target_sec = sec_part.strip()
                key_name = rem_key.strip()

        if "\n" in key_name or "\r" in key_name or "\n" in val_str or "\r" in val_str:
            raise ValueError("Las claves y valores INI no pueden contener saltos de línea.")

        return target_sec, key_name, val_str

    @classmethod
    def _update_ini_conservatively(
        cls,
        original_text: str,
        key_values: dict[str, Any],
        *,
        default_section: Optional[str] = None,
        separator: str = "=",
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Actualiza un texto INI/CFG línea por línea de forma conservadora sin usar `configparser`:
        - Preserva mayúsculas/minúsculas de claves y secciones (búsqueda case-sensitive exacta).
        - Preserva comentarios (`;` y `#`), líneas vacías, orden de secciones y saltos `CRLF`/`LF`.
        - Soporta archivos sin sección inicial.
        - Rechaza claves duplicadas dentro de una misma sección o coincidencias ambiguas entre secciones.
        """
        if not key_values:
            raise ValueError("No se especificaron pares clave-valor para actualizar.")
        if separator not in ("=", ":"):
            raise ValueError(f"Separador '{separator}' no admitido para formato INI/CFG.")

        lines = original_text.splitlines(keepends=True)
        dominant_nl = _detect_dominant_newline(original_text)

        # Expresión regular para una línea de asignación con el separador configurado
        sep_escaped = re.escape(separator)
        kv_regex = re.compile(
            rf"^(?P<indent>\s*)(?P<key>[^\[;#={sep_escaped}\s][^;#={sep_escaped}\r\n]*?)"
            rf"(?P<sep>\s*{sep_escaped}\s*)(?P<rest>.*)$"
        )

        # Primer pase: indexar secciones y posiciones de cada clave
        current_section: Optional[str] = None
        section_ranges: dict[Optional[str], int] = {None: -1}
        # Mapa: (section_name, key_exact) -> lista de índices de línea
        key_locations: dict[tuple[Optional[str], str], list[int]] = {}
        # Mapa por clave sin sección: key_exact -> lista de (section_name, line_idx)
        key_by_name_only: dict[str, list[tuple[Optional[str], int]]] = {}

        for idx, raw_line in enumerate(lines):
            body, _ = _split_line_ending(raw_line)
            stripped = body.strip()
            if not stripped or stripped.startswith((";", "#")):
                continue

            sec_match = _INI_SECTION_RE.match(body)
            if sec_match:
                current_section = sec_match.group("section").strip()
                section_ranges[current_section] = idx
                continue

            kv_match = kv_regex.match(body)
            if kv_match:
                k_exact = kv_match.group("key").strip()
                pair = (current_section, k_exact)
                key_locations.setdefault(pair, []).append(idx)
                key_by_name_only.setdefault(k_exact, []).append((current_section, idx))
                section_ranges[current_section] = idx

        applied_changes: list[dict[str, Any]] = []
        updated_lines = list(lines)

        for raw_k, raw_v in key_values.items():
            target_sec, k_name, new_val_str = cls._parse_key_spec(raw_k, raw_v, default_section)

            # Caso 1: el usuario o el plan especificó sección explícita
            if target_sec is not None:
                locs = key_locations.get((target_sec, k_name), [])
                if len(locs) > 1:
                    raise ValueError(
                        f"Clave duplicada detectada: '{k_name}' aparece {len(locs)} veces en la sección '[{target_sec}]'."
                    )
                if len(locs) == 1:
                    line_idx = locs[0]
                    old_val, new_line = cls._replace_value_in_ini_line(
                        updated_lines[line_idx], kv_regex, new_val_str, dominant_nl
                    )
                    updated_lines[line_idx] = new_line
                    applied_changes.append(
                        {
                            "section": target_sec,
                            "key": k_name,
                            "old_value": old_val,
                            "new_value": new_val_str,
                        }
                    )
                else:
                    raise ValueError(
                        f"La clave '{k_name}' en la sección '[{target_sec}]' no existe en el archivo (respetando mayúsculas/minúsculas)."
                    )
            else:
                # Caso 2: no se indicó sección; verificamos que no exista ambigüedad ni duplicados
                occurrences = key_by_name_only.get(k_name, [])
                if len(occurrences) == 0:
                    raise ValueError(
                        f"La clave '{k_name}' no fue encontrada en el archivo (búsqueda case-sensitive)."
                    )
                if len(occurrences) > 1:
                    sections_found = [sec or "<global>" for sec, _ in occurrences]
                    if len(set(sections_found)) == 1:
                        raise ValueError(
                            f"Clave duplicada detectada: '{k_name}' aparece {len(occurrences)} veces en '{sections_found[0]}'."
                        )
                    raise ValueError(
                        f"Coincidencia ambigua: la clave '{k_name}' existe en múltiples secciones ({', '.join(sections_found)}). Especifique la sección."
                    )

                found_sec, line_idx = occurrences[0]
                old_val, new_line = cls._replace_value_in_ini_line(
                    updated_lines[line_idx], kv_regex, new_val_str, dominant_nl
                )
                updated_lines[line_idx] = new_line
                applied_changes.append(
                    {
                        "section": found_sec,
                        "key": k_name,
                        "old_value": old_val,
                        "new_value": new_val_str,
                    }
                )

        return "".join(updated_lines), applied_changes

    @staticmethod
    def _replace_value_in_ini_line(
        raw_line: str,
        kv_regex: re.Pattern[str],
        new_value: str,
        default_nl: str,
    ) -> tuple[str, str]:
        """
        Sustituye únicamente el valor de una línea INI conservando la indentación, el nombre exacto
        de la clave, el espaciado alrededor de `=`, cualquier comentario inline posterior y el salto de línea.
        """
        body, line_ending = _split_line_ending(raw_line)
        match = kv_regex.match(body)
        if not match:
            raise ValueError("Línea INI no coincide con el patrón esperado.")

        indent = match.group("indent")
        key_part = match.group("key")
        sep_part = match.group("sep")
        rest = match.group("rest")

        # Separamos el valor actual de un posible comentario inline (ej. `1920 ; comentario`)
        inline_comment = ""
        val_part = rest
        in_quotes: Optional[str] = None
        for idx, ch in enumerate(rest):
            if ch in ('"', "'"):
                if in_quotes is None:
                    in_quotes = ch
                elif in_quotes == ch:
                    in_quotes = None
            elif ch in (";", "#") and in_quotes is None:
                if idx == 0 or rest[idx - 1].isspace():
                    # Conservamos los espacios previos al comentario inline
                    split_pos = idx
                    while split_pos > 0 and rest[split_pos - 1] in (" ", "\t"):
                        split_pos -= 1
                    val_part = rest[:split_pos]
                    inline_comment = rest[split_pos:]
                    break

        old_value = val_part.strip()
        rebuilt = f"{indent}{key_part}{sep_part}{new_value}{inline_comment}{line_ending or default_nl if line_ending else ''}"
        return old_value, rebuilt

    @staticmethod
    def _update_json_conservatively(
        original_text: str,
        key_values: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Parsea, valida y actualiza un documento JSON preservando los tipos de datos y el salto de línea.
        Rechaza documentos JSON inválidos o rutas que no apunten a un objeto JSON.
        """
        dominant_nl = _detect_dominant_newline(original_text)
        try:
            data = json.loads(original_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"El archivo JSON original no es válido: {exc.msg}.") from exc

        if not isinstance(data, dict):
            raise ValueError("El documento JSON raíz debe ser un objeto clave-valor.")

        applied_changes: list[dict[str, Any]] = []
        for raw_key, new_val in key_values.items():
            actual_val = new_val["value"] if (isinstance(new_val, dict) and "value" in new_val) else new_val
            parts = [p for p in str(raw_key).split(".") if p]
            if not parts:
                raise ValueError("Clave JSON vacía.")

            cursor: Any = data
            for part in parts[:-1]:
                if not isinstance(cursor, dict) or part not in cursor or not isinstance(cursor[part], dict):
                    raise ValueError(f"La ruta JSON '{raw_key}' no existe en el documento.")
                cursor = cursor[part]

            leaf_key = parts[-1]
            if not isinstance(cursor, dict) or leaf_key not in cursor:
                raise ValueError(f"La clave JSON '{raw_key}' no existe en el documento.")

            old_val = cursor[leaf_key]
            cursor[leaf_key] = actual_val
            applied_changes.append(
                {
                    "section": ".".join(parts[:-1]) or None,
                    "key": leaf_key,
                    "old_value": old_val,
                    "new_value": actual_val,
                }
            )

        serialized = json.dumps(data, indent=2, ensure_ascii=False)
        # Re-validamos que el JSON generado sea sintácticamente válido
        json.loads(serialized)
        if dominant_nl == "\r\n":
            serialized = serialized.replace("\r\n", "\n").replace("\n", "\r\n")
        if original_text.endswith(("\r\n", "\n")) and not serialized.endswith(("\r\n", "\n")):
            serialized += dominant_nl
        return serialized, applied_changes

    @staticmethod
    def _update_xml_safely(
        original_text: str,
        key_values: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Valida y actualiza un documento XML previniendo ataques XXE (bloquea `<!DOCTYPE` y `<!ENTITY`).
        Soporta actualizar el texto de un nodo (`Tag` o `Ruta/SubTag`) o un atributo (`Tag@attr`).
        """
        if _XML_XXE_RE.search(original_text):
            raise PermissionError(
                "Operación XML rechazada por seguridad: se detectaron declaraciones DTD/ENTITY (prevención XXE)."
            )

        try:
            root = ET.fromstring(original_text)
        except ET.ParseError as exc:
            raise ValueError(f"El documento XML original no es válido: {exc}.") from exc

        applied_changes: list[dict[str, Any]] = []
        for raw_key, raw_val in key_values.items():
            new_val_str = str(raw_val["value"] if (isinstance(raw_val, dict) and "value" in raw_val) else raw_val)
            if _XML_XXE_RE.search(new_val_str):
                raise PermissionError("Valor XML rechazado por contener declaraciones DTD/ENTITY prohibidas.")

            key_str = str(raw_key).strip()
            attr_name: Optional[str] = None
            if "@" in key_str:
                xpath_part, attr_name = key_str.split("@", 1)
                xpath_part = xpath_part.strip() or "."
                attr_name = attr_name.strip()
            else:
                xpath_part = key_str

            elements = [root] if xpath_part in (".", root.tag) else root.findall(xpath_part)
            if len(elements) == 0:
                raise ValueError(f"No se encontró el elemento XML '{xpath_part}'.")
            if len(elements) > 1:
                raise ValueError(f"Coincidencia XML ambigua: '{xpath_part}' coincide con {len(elements)} elementos.")

            elem = elements[0]
            if attr_name:
                if attr_name not in elem.attrib:
                    raise ValueError(f"El atributo '@{attr_name}' no existe en el elemento '{elem.tag}'.")
                old_val = elem.attrib[attr_name]
                elem.set(attr_name, new_val_str)
            else:
                old_val = elem.text or ""
                elem.text = new_val_str

            applied_changes.append(
                {
                    "section": xpath_part,
                    "key": f"@{attr_name}" if attr_name else elem.tag,
                    "old_value": old_val,
                    "new_value": new_val_str,
                }
            )

        xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=original_text.lstrip().startswith("<?xml"))
        xml_str = xml_bytes.decode("utf-8")
        # Validamos que el XML resultante siga siendo válido y sin XXE
        ET.fromstring(xml_str)
        return xml_str, applied_changes

    # ------------------------------------------------------------------
    # Escritura atómica en Windows con reintentos ante bloqueos transitorios
    # ------------------------------------------------------------------
    @staticmethod
    def _is_transient_windows_lock_error(error: BaseException) -> bool:
        """
        Distingue un bloqueo transitorio de archivo en Windows (`PermissionError`, `WinError 32`
        o `WinError 5` durante `os.replace`) de errores permanentes (`FileNotFoundError`, `ENOSPC`, etc.).
        """
        if isinstance(error, (FileNotFoundError, IsADirectoryError, NotADirectoryError)):
            return False
        winerr = getattr(error, "winerror", None)
        if winerr in (5, 32, 33):
            return True
        if isinstance(error, PermissionError):
            return True
        err_no = getattr(error, "errno", None)
        return err_no in (errno.EACCES, errno.EBUSY)

    def _atomic_write_bytes_with_retries(
        self,
        target_path: Path,
        new_bytes: bytes,
        expected_original_hash: str,
        *,
        format_validator: Optional[Callable[[bytes], None]] = None,
    ) -> tuple[str, int]:
        """
        Ejecuta la secuencia de escritura atómica en el mismo directorio del archivo objetivo:
        1. Crea temporal único en `target_path.parent`.
        2. Escribe `new_bytes`, ejecuta `flush()` + `os.fsync()`.
        3. Valida el contenido temporal (`format_validator` y hash).
        4. Revalida la ruta y verifica que el hash actual del archivo original siga siendo `expected_original_hash`.
        5. Reemplaza con `os.replace` aplicando hasta `self.max_retries` intentos (100 ms base) ante bloqueos transitorios.
        6. Re-lee el archivo final y comprueba su SHA-256.
        """
        parent_dir = target_path.parent
        temp_name = f".{target_path.name}.{uuid.uuid4().hex[:10]}.tmp"
        temp_path = parent_dir / temp_name
        expected_new_hash = compute_sha256_bytes(new_bytes)

        try:
            with open(temp_path, "xb") as tf:
                tf.write(new_bytes)
                tf.flush()
                os.fsync(tf.fileno())

            # Preservar permisos/modo básico cuando el sistema lo permita
            try:
                st_orig = os.stat(target_path)
                os.chmod(temp_path, stat.S_IMODE(st_orig.st_mode))
            except OSError:
                pass

            # Validación del archivo temporal escrito en disco
            temp_read = temp_path.read_bytes()
            if compute_sha256_bytes(temp_read) != expected_new_hash:
                raise RuntimeError("El archivo temporal escrito no superó la verificación SHA-256.")
            if format_validator is not None:
                format_validator(temp_read)

            # Revalidación anti-TOCTOU de la ruta y detección de modificación concurrente
            revalidated_target = self.validate_authorized_path(
                target_path,
                must_exist=True,
                require_regular_file=True,
            )
            current_target_bytes = revalidated_target.read_bytes()
            current_target_hash = compute_sha256_bytes(current_target_bytes)
            if current_target_hash != expected_original_hash:
                raise BufferError(
                    "Conflicto de concurrencia: el archivo objetivo fue modificado por otro proceso antes de aplicar los cambios."
                )

            # Reemplazo atómico con política acotada de reintentos ante WinError 32 / PermissionError
            attempts = 0
            last_exc: Optional[BaseException] = None
            for attempt in range(1, self.max_retries + 1):
                attempts = attempt
                try:
                    os.replace(str(temp_path), str(revalidated_target))
                    last_exc = None
                    break
                except OSError as exc:
                    last_exc = exc
                    if not self._is_transient_windows_lock_error(exc) or attempt >= self.max_retries:
                        break
                    delay_sec = (self.retry_base_ms * (2 ** (attempt - 1))) / 1000.0
                    self._sleep_fn(delay_sec)

            if last_exc is not None:
                raise RuntimeError(
                    f"No se pudo reemplazar el archivo tras {attempts} intento(s) debido a bloqueo en Windows ({type(last_exc).__name__})."
                ) from last_exc

            # Verificación post-escritura leyendo el archivo final
            final_bytes = revalidated_target.read_bytes()
            final_hash = compute_sha256_bytes(final_bytes)
            if final_hash != expected_new_hash:
                raise RuntimeError("Fallo de verificación post-escritura: el hash final no coincide con el esperado.")

            return final_hash, attempts

        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def modify_ini_or_text(
        self,
        file_path: str | Path,
        key_values: Optional[dict[str, Any]] = None,
        replacement_content: Optional[str] = None,
        *,
        section: Optional[str] = None,
        execution_id: Optional[str] = None,
        expected_file_hash: Optional[str] = None,
        separator: str = "=",
    ) -> dict[str, Any]:
        """
        Modifica de manera segura un archivo de configuración autorizado (`.ini`, `.cfg`, `.txt`, `.json`, `.xml`):
        1. Rechaza argumentos contradictorios (`key_values` y `replacement_content` a la vez, o ambos vacíos).
        2. Valida la ruta y verifica `expected_file_hash` si fue proporcionado.
        3. Prepara y valida en memoria el nuevo contenido preservando formato, BOM y codificación.
        4. Crea y verifica el backup SHA-256 antes de tocar el archivo.
        5. Escribe atómicamente con reintentos y verifica el resultado final.
        """
        exec_id = execution_id or generate_execution_id()
        errors: list[str] = []
        warnings: list[str] = []

        # Validación de parámetros mutuamente excluyentes
        has_kv = key_values is not None and len(key_values) > 0
        has_rep = replacement_content is not None
        if (has_kv and has_rep) or (not has_kv and not has_rep):
            err_msg = (
                "Parámetros contradictorios o incompletos: debe indicar 'key_values' O 'replacement_content', "
                "nunca ambos simultáneamente ni ambos vacíos."
            )
            result = {
                "status": "error",
                "file_path": str(file_path),
                "original_hash": None,
                "final_hash": None,
                "backup_path": None,
                "applied_changes": [],
                "errors": [err_msg],
                "warnings": warnings,
                "execution_id": exec_id,
            }
            self.last_operation_result = result
            return result

        try:
            validated_target = self.validate_authorized_path(
                file_path,
                must_exist=True,
                require_regular_file=True,
            )
            original_bytes = validated_target.read_bytes()
            original_hash = compute_sha256_bytes(original_bytes)

            if expected_file_hash and expected_file_hash.strip().lower() != original_hash.lower():
                conflict_msg = (
                    "Conflicto de concurrencia: el hash actual del archivo difiere del hash esperado en el plan."
                )
                result = {
                    "status": "conflict",
                    "file_path": str(validated_target),
                    "original_hash": original_hash,
                    "final_hash": original_hash,
                    "backup_path": None,
                    "applied_changes": [],
                    "errors": [conflict_msg],
                    "warnings": warnings,
                    "execution_id": exec_id,
                }
                self.audit_logger.record("modify_conflict", exec_id, result)
                self.last_operation_result = result
                return result

            decoded_text, codec, bom = _decode_bytes_strict(original_bytes)
            ext = validated_target.suffix.lower()

            applied_changes: list[dict[str, Any]] = []
            if has_rep:
                assert replacement_content is not None
                if ext == ".json":
                    try:
                        json.loads(replacement_content)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"El reemplazo JSON propuesto no es válido: {exc.msg}.") from exc
                elif ext == ".xml":
                    if _XML_XXE_RE.search(replacement_content):
                        raise PermissionError("Reemplazo XML bloqueado por contener declaraciones DTD/ENTITY (XXE).")
                    try:
                        ET.fromstring(replacement_content)
                    except ET.ParseError as exc:
                        raise ValueError(f"El reemplazo XML propuesto no es válido: {exc}.") from exc

                new_text = replacement_content
                applied_changes.append(
                    {
                        "section": None,
                        "key": "__full_replacement__",
                        "old_value": f"<{len(decoded_text)} chars>",
                        "new_value": f"<{len(new_text)} chars>",
                    }
                )
            else:
                assert key_values is not None
                if ext == ".json":
                    new_text, applied_changes = self._update_json_conservatively(decoded_text, key_values)
                elif ext == ".xml":
                    new_text, applied_changes = self._update_xml_safely(decoded_text, key_values)
                elif ext in (".ini", ".cfg", ".txt", ".conf"):
                    new_text, applied_changes = self._update_ini_conservatively(
                        decoded_text,
                        key_values,
                        default_section=section,
                        separator=separator,
                    )
                else:
                    raise ValueError(f"Formato de archivo '{ext}' no soportado para actualización por claves.")

            new_bytes = _encode_text_strict(new_text, codec, bom)

            # Solo después de validar que el nuevo contenido es válido y codificable creamos el backup
            backup_path = self.create_backup(validated_target, execution_id=exec_id)

            def _validate_temp_bytes(raw_b: bytes) -> None:
                txt, _, _ = _decode_bytes_strict(raw_b)
                if ext == ".json":
                    json.loads(txt)
                elif ext == ".xml":
                    if _XML_XXE_RE.search(txt):
                        raise PermissionError("XXE detectado en validación temporal.")
                    ET.fromstring(txt)

            final_hash, attempts = self._atomic_write_bytes_with_retries(
                validated_target,
                new_bytes,
                original_hash,
                format_validator=_validate_temp_bytes,
            )
            self.backup_manager.update_post_edit_hash(backup_path, final_hash)

            if attempts > 1:
                warnings.append(f"El reemplazo atómico requirió {attempts} intentos por bloqueo transitorio.")

            result = {
                "status": "success",
                "file_path": str(validated_target),
                "original_hash": original_hash,
                "final_hash": final_hash,
                "backup_path": str(backup_path),
                "applied_changes": applied_changes,
                "errors": [],
                "warnings": warnings,
                "execution_id": exec_id,
            }
            self.audit_logger.record("modify_success", exec_id, result)
            self.last_operation_result = result
            return result

        except BufferError as conflict_exc:
            result = {
                "status": "conflict",
                "file_path": str(file_path),
                "original_hash": locals().get("original_hash"),
                "final_hash": None,
                "backup_path": str(locals().get("backup_path")) if "backup_path" in locals() else None,
                "applied_changes": [],
                "errors": [redact_secrets(str(conflict_exc))],
                "warnings": warnings,
                "execution_id": exec_id,
            }
            self.audit_logger.record("modify_conflict", exec_id, result)
            self.last_operation_result = result
            return result

        except Exception as exc:
            errors.append(redact_secrets(str(exc)))
            result = {
                "status": "error",
                "file_path": str(file_path),
                "original_hash": locals().get("original_hash"),
                "final_hash": None,
                "backup_path": str(locals().get("backup_path")) if "backup_path" in locals() else None,
                "applied_changes": [],
                "errors": errors,
                "warnings": warnings,
                "execution_id": exec_id,
            }
            self.audit_logger.record("modify_error", exec_id, result)
            self.last_operation_result = result
            return result

    def rollback(
        self,
        backup_path: Path | str,
        target_path: Path | str,
        *,
        expected_current_hash: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> bool:
        """
        Restaura de forma segura un archivo objetivo a partir de su backup `.bak`:
        1. Valida que tanto `backup_path` como `target_path` sean rutas seguras y autorizadas.
        2. Verifica la integridad SHA-256 del backup contra su registro original (si existe en `BackupManager`).
        3. Detecta modificaciones posteriores ajenas a la ejecución original (`expected_current_hash` o `record.post_edit_sha256`).
        4. Crea un backup del estado actual antes de restaurar.
        5. Reemplaza de forma atómica y verifica el hash final restaurado.
        """
        exec_id = execution_id or generate_execution_id()
        try:
            validated_backup = self.validate_authorized_path(
                backup_path,
                must_exist=True,
                require_regular_file=True,
            )
            validated_target = self.validate_authorized_path(
                target_path,
                must_exist=True,
                require_regular_file=True,
            )

            backup_bytes = validated_backup.read_bytes()
            backup_hash = compute_sha256_bytes(backup_bytes)

            record = self.backup_manager.get_record_by_backup(validated_backup)
            if record is not None and record.backup_sha256 != backup_hash:
                raise RuntimeError("El archivo de backup está corrupto: su hash SHA-256 no coincide con el registrado.")

            current_target_bytes = validated_target.read_bytes()
            current_target_hash = compute_sha256_bytes(current_target_bytes)

            # Detección de cambios posteriores realizados por el usuario después de la edición original
            expected_hash_to_check = expected_current_hash or (
                record.post_edit_sha256 if record is not None else None
            )
            if expected_hash_to_check and current_target_hash.lower() != expected_hash_to_check.lower():
                self.audit_logger.record(
                    "rollback_conflict",
                    exec_id,
                    {
                        "target_path": str(validated_target),
                        "backup_path": str(validated_backup),
                        "current_hash": current_target_hash,
                        "expected_hash": expected_hash_to_check,
                    },
                )
                self.last_operation_result = {
                    "status": "conflict",
                    "file_path": str(validated_target),
                    "backup_path": str(validated_backup),
                    "errors": [
                        "Rollback detenido: el archivo fue modificado con posterioridad a la ejecución original."
                    ],
                    "execution_id": exec_id,
                }
                return False

            # Creamos un backup preventivo del estado actual antes de restaurar
            pre_rollback_bak = self.create_backup(validated_target, execution_id=f"{exec_id}_prerollback")

            final_hash, _ = self._atomic_write_bytes_with_retries(
                validated_target,
                backup_bytes,
                current_target_hash,
            )
            if final_hash != backup_hash:
                return False

            self.audit_logger.record(
                "rollback_success",
                exec_id,
                {
                    "target_path": str(validated_target),
                    "restored_from": str(validated_backup),
                    "pre_rollback_backup": str(pre_rollback_bak),
                    "restored_sha256": final_hash,
                },
            )
            self.last_operation_result = {
                "status": "rolled_back",
                "file_path": str(validated_target),
                "backup_path": str(validated_backup),
                "pre_rollback_backup": str(pre_rollback_bak),
                "final_hash": final_hash,
                "errors": [],
                "execution_id": exec_id,
            }
            return True

        except Exception as exc:
            self.audit_logger.record(
                "rollback_error",
                exec_id,
                {
                    "target_path": str(target_path),
                    "backup_path": str(backup_path),
                    "error": redact_secrets(str(exc)),
                },
            )
            self.last_operation_result = {
                "status": "error",
                "file_path": str(target_path),
                "backup_path": str(backup_path),
                "errors": [redact_secrets(str(exc))],
                "execution_id": exec_id,
            }
            return False

    def execute(self, params: Optional[dict[str, Any]] = None, **kwargs: Any) -> str:
        """
        Ejecuta una acción de `FileEditorSkill` a partir de un diccionario `params` o `**kwargs`
        y devuelve una respuesta en lenguaje natural con trazabilidad (`execution_id` y hashes cortos),
        sin exponer trazas internas ni rutas privadas completas.
        """
        merged: dict[str, Any] = {}
        if isinstance(params, dict):
            merged.update(params)
        merged.update(kwargs)

        action = str(merged.get("action") or "").strip().lower()
        exec_id = str(merged.get("execution_id") or generate_execution_id())

        if action == "discover":
            app_name = str(merged.get("game_or_app_name") or "")
            target_file = str(merged.get("target_filename") or "")
            info = self.discover_config_files_detailed(app_name, target_file)
            status = info.get("status")
            if status == "found":
                found_name = Path(str(info["selected_path"])).name
                return f"Archivo '{found_name}' localizado de forma inequívoca para '{app_name}', señor."
            if status == "ambiguous":
                count = len(info.get("matches") or [])
                return (
                    f"Se detectaron {count} archivos '{target_file}' en distintas ubicaciones para '{app_name}', señor. "
                    "Por seguridad necesito que seleccione cuál instalación desea utilizar."
                )
            return f"No encontré el archivo '{target_file}' para '{app_name}' dentro de las zonas autorizadas, señor."

        if action == "backup":
            raw_path = merged.get("file_path")
            if not raw_path:
                return "Por favor indique el archivo objetivo para crear el respaldo, señor."
            try:
                bak_path = self.create_backup(str(raw_path), execution_id=exec_id)
                rec = self.backup_manager.get_record_by_backup(bak_path)
                short_hash = rec.backup_sha256[:12] if rec else "verificado"
                return (
                    f"Respaldo '{bak_path.name}' creado y verificado con éxito "
                    f"(SHA-256: {short_hash}..., ID: {exec_id}), señor."
                )
            except Exception as exc:
                return f"No se pudo crear el respaldo solicitado, señor: {redact_secrets(str(exc))}"

        if action == "modify":
            raw_path = merged.get("file_path")
            if not raw_path:
                return "Por favor indique el archivo que desea modificar, señor."
            res = self.modify_ini_or_text(
                file_path=str(raw_path),
                key_values=merged.get("key_values"),
                replacement_content=merged.get("replacement_content"),
                section=merged.get("section"),
                execution_id=exec_id,
                expected_file_hash=merged.get("expected_file_hash"),
            )
            if res["status"] == "success":
                fname = Path(str(res["file_path"])).name
                bak_name = Path(str(res["backup_path"])).name if res.get("backup_path") else "N/A"
                short_final = str(res["final_hash"])[:12]
                count = len(res.get("applied_changes") or [])
                return (
                    f"Se aplicaron y verificaron {count} cambio(s) en '{fname}' "
                    f"(respaldo: '{bak_name}', SHA-256 final: {short_final}..., ID: {res['execution_id']}), señor."
                )
            err_summary = "; ".join(res.get("errors") or ["Operación rechazada por política de seguridad."])
            return f"No se modificó el archivo, señor: {err_summary} (ID: {res['execution_id']})."

        if action == "rollback":
            bak = merged.get("backup_path")
            tgt = merged.get("file_path") or merged.get("target_path")
            if not bak or not tgt:
                return "Para restaurar un archivo necesito la ruta del backup y la del archivo objetivo, señor."
            ok = self.rollback(
                backup_path=str(bak),
                target_path=str(tgt),
                expected_current_hash=merged.get("expected_current_hash"),
                execution_id=exec_id,
            )
            if ok:
                fname = Path(str(tgt)).name
                return f"El archivo '{fname}' fue restaurado y verificado exitosamente desde su respaldo (ID: {exec_id}), señor."
            err_list = self.last_operation_result.get("errors") or ["No fue posible verificar la restauración segura."]
            return f"Restauración detenida por seguridad, señor: {'; '.join(err_list)} (ID: {exec_id})."

        if action == "read":
            raw_path = merged.get("file_path")
            if not raw_path:
                return "Por favor indique el archivo que desea leer, señor."
            try:
                validated = self.validate_authorized_path(str(raw_path), must_exist=True, require_regular_file=True)
                raw_bytes = validated.read_bytes()
                sha = compute_sha256_bytes(raw_bytes)[:12]
                return f"Archivo '{validated.name}' verificado ({len(raw_bytes)} bytes, SHA-256: {sha}...), señor."
            except Exception as exc:
                return f"No fue posible leer el archivo solicitado, señor: {redact_secrets(str(exc))}"

        return (
            f"Acción '{action}' no reconocida en FileEditorSkill, señor. "
            "Las acciones válidas son: discover, read, backup, modify y rollback."
        )


class RollbackManager:
    """Coordinador de alto nivel para restaurar lotes completos de `FileEditorSkill` por `execution_id`."""

    def __init__(self, editor_skill: FileEditorSkill) -> None:
        self.editor_skill: FileEditorSkill = editor_skill

    def rollback_execution(self, execution_id: str) -> dict[str, Any]:
        """Restaura en orden inverso todos los archivos modificados bajo un `execution_id`."""
        records = self.editor_skill.backup_manager.get_records_by_execution(execution_id)
        if not records:
            return {
                "status": "error",
                "execution_id": execution_id,
                "restored_files": [],
                "errors": [f"No existen backups registrados para el identificador '{execution_id}'."],
            }

        restored: list[str] = []
        errors: list[str] = []
        for rec in reversed(records):
            ok = self.editor_skill.rollback(
                backup_path=rec.backup_path,
                target_path=rec.target_path,
                expected_current_hash=rec.post_edit_sha256,
                execution_id=f"{execution_id}_rollback",
            )
            if ok:
                restored.append(rec.target_path)
            else:
                err_detail = "; ".join(self.editor_skill.last_operation_result.get("errors") or ["Conflicto al restaurar"])
                errors.append(f"{Path(rec.target_path).name}: {err_detail}")

        return {
            "status": "rolled_back" if (restored and not errors) else ("partial_error" if restored else "error"),
            "execution_id": execution_id,
            "restored_files": restored,
            "errors": errors,
        }
